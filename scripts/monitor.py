"""One monitoring cycle: fetch Stories -> classify new ones -> dedupe jobs -> WhatsApp.

Usage:
  uv run python -m scripts.monitor              # one cycle (respects DRY_RUN in .env)
  uv run python -m scripts.monitor --dry-run    # never send WhatsApp; print messages instead
  uv run python -m scripts.monitor --live       # send even if .env says DRY_RUN=true
  uv run python -m scripts.monitor --baseline   # mark current Stories as seen without classifying
  uv run python -m scripts.monitor --limit 3    # classify at most 3 new Stories this run

The 30-minute OpenClaw automation runs this same command. It exits 0 on success, 1 on failure.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from scripts import instagram
from scripts.database import Database
from scripts.job_extractor import Classification, ExtractionError, classify_story, event_rejection, job_rejection
from scripts.notifier import NotificationError, format_message, send_whatsapp
from scripts.settings import Settings, load_settings

log = logging.getLogger("monitor")

FAILURE_ALERT_THRESHOLD = 3  # consecutive failed runs (~90 min) before alerting over WhatsApp

AUTH_EXPIRED_MSG = (
    "Instagram authentication has expired.\n"
    "Please reauthenticate the persistent browser session:\n"
    "uv run python -m scripts.instagram_login"
)


@dataclass
class FetchedStory:
    story: instagram.Story
    image_path: Path


@dataclass
class Report:
    stories_found: int = 0
    already_processed: int = 0
    new_stories: int = 0
    skipped_viewed: int = 0
    classified: int = 0
    irrelevant: int = 0
    new_jobs: list[str] = field(default_factory=list)
    new_events: list[str] = field(default_factory=list)
    filtered_out: list[str] = field(default_factory=list)
    duplicate_jobs: list[str] = field(default_factory=list)
    messages_sent: int = 0
    errors: list[str] = field(default_factory=list)


# Dependency seams so tests can mock Instagram, the LLM, and WhatsApp.
Fetcher = Callable[[Settings, Database], list[FetchedStory]]
Classifier = Callable[[Path, str | None, str, dict], Classification]
Sender = Callable[[str, str], str]


def fetch_with_browser(settings: Settings, db: Database) -> list[FetchedStory]:
    """Fetch active Stories; download images only for ones that still need processing."""
    with instagram.open_browser(settings.browser_profile_dir, headless=True) as context:
        log.info("Checking %s", settings.instagram_username)
        stories = instagram.fetch_stories(context, settings.instagram_username)
        log.info("Instagram session authenticated")
        fetched = []
        for story in stories:
            db.record_story(story.story_key, story.taken_at, story.link_url)
            path = settings.screenshots_dir / f"{story.story_key}.jpg"
            if db.needs_processing(story.story_key) and not story.viewed and not path.exists():
                instagram.download_image(context, story.image_url, path)
            fetched.append(FetchedStory(story, path))
    _prune_images(settings.screenshots_dir)
    return fetched


def _prune_images(directory: Path, max_age_days: int = 7) -> None:
    """Stories expire after 24h; keep images a week for debugging, then delete."""
    cutoff = time.time() - max_age_days * 86400
    for path in directory.glob("*.jpg"):
        if path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)


def run_cycle(
    settings: Settings,
    db: Database,
    *,
    dry_run: bool,
    baseline: bool = False,
    limit: int | None = None,
    fetcher: Fetcher = fetch_with_browser,
    classifier: Classifier = classify_story,
    sender: Sender = send_whatsapp,
) -> Report:
    report = Report()
    log.info("Starting Instagram monitor%s", " (dry run)" if dry_run else "")

    try:
        fetched = fetcher(settings, db)
    except instagram.AuthExpiredError:
        log.error("Instagram authentication has expired")
        report.errors.append("auth_expired")
        _alert_once(db, "auth_alert_sent", AUTH_EXPIRED_MSG, settings, dry_run, sender)
        _record_failure(db, settings, dry_run, sender)
        return report
    except Exception as e:  # network, page changes, browser crashes
        log.error("Instagram check failed: %s. Will retry during the next scheduled run.", e)
        report.errors.append(f"fetch_failed: {e}")
        _record_failure(db, settings, dry_run, sender)
        return report

    db.set_state("auth_alert_sent", None)
    for f in fetched:  # idempotent; the fetcher may already have recorded them
        db.record_story(f.story.story_key, f.story.taken_at, f.story.link_url)
    report.stories_found = len(fetched)
    pending = [f for f in fetched if db.needs_processing(f.story.story_key)]
    report.already_processed = report.stories_found - len(pending)
    log.info("Found %d active Stories", report.stories_found)
    log.info("%d Stories already processed", report.already_processed)

    viewed = [f for f in pending if f.story.viewed]
    for f in viewed:
        db.mark_skipped(f.story.story_key, "already viewed on Instagram")
    if viewed:
        log.info("%d Stories already viewed on Instagram; skipping without analysis", len(viewed))
    pending = [f for f in pending if not f.story.viewed]
    report.skipped_viewed = len(viewed)
    report.new_stories = len(pending)
    log.info("%d new Stories detected", report.new_stories)

    if baseline:
        for f in pending:
            db.mark_skipped(f.story.story_key, "baseline")
        log.info("Baseline: marked %d Stories as seen without classifying", len(pending))
        pending = []

    if limit is not None and len(pending) > limit:
        log.info("Limiting this run to %d of %d new Stories", limit, len(pending))
        pending = pending[:limit]

    filtering = settings.config.get("filtering", {})
    for f in pending:
        _process_story(f, settings, db, report, classifier, filtering)

    _send_pending(settings, db, report, dry_run, sender)

    if not report.errors:
        db.set_state("consecutive_failures", "0")
        db.set_state("failure_alert_sent", None)
    log.info("Done: %s", summarize(report))
    return report


def _process_story(f: FetchedStory, settings, db: Database, report: Report, classifier, filtering) -> None:
    key = f.story.story_key
    try:
        if not f.image_path.exists():
            raise ExtractionError("Story image was not downloaded")
        result = classifier(f.image_path, f.story.link_url, settings.llm_model, filtering)
    except ExtractionError as e:
        gave_up = db.mark_failed(key, str(e))
        log.error("Story %s: classification failed (%s)%s", key, e, "; giving up" if gave_up else "; will retry")
        report.errors.append(f"classify_failed: {key}")
        return

    content_hash = hashlib.sha256(f.image_path.read_bytes()).hexdigest()
    db.mark_classified(
        key, is_job=result.is_job, relevance=result.relevance, confidence=result.confidence,
        raw=repr(result), content_hash=content_hash,
    )
    report.classified += 1

    if not result.jobs and not result.events:
        report.irrelevant += 1
        log.info("Story %s classified as not a job or event (%s)", key, result.relevance)
        return

    kinds = " + ".join(k for k, present in (("job opportunity", result.jobs), ("event", result.events)) if present)
    log.info("Story %s classified as %s (%s)", key, kinds, result.relevance)
    use_url_key = len(result.jobs) + len(result.events) == 1

    for job in result.jobs:
        label = f"{job.company or '?'} - {job.role or '?'}"
        log.info("Extracted: %s", label)
        reason = job_rejection(job, filtering)
        if reason:
            log.info("Filtered out (%s): %s", reason, label)
            report.filtered_out.append(f"{label} ({reason})")
        elif db.add_job(job, key, use_url_key) is None:
            log.info("Job is a duplicate: %s", label)
            report.duplicate_jobs.append(label)
        else:
            log.info("Job is new: %s", label)
            report.new_jobs.append(label)

    for event in result.events:
        label = f"{event.organizer or '?'} - {event.name or '?'}"
        log.info("Extracted event: %s", label)
        reason = event_rejection(event, filtering)
        if reason:
            log.info("Filtered out (%s): %s", reason, label)
            report.filtered_out.append(f"{label} ({reason})")
        elif db.add_event(event, key, use_url_key) is None:
            log.info("Event is a duplicate: %s", label)
            report.duplicate_jobs.append(label)
        else:
            log.info("Event is new: %s", label)
            report.new_events.append(label)


def _send_pending(settings: Settings, db: Database, report: Report, dry_run: bool, sender: Sender) -> None:
    """Send every unsent job (this run's plus earlier failed deliveries), one message per Story."""
    for _group, rows in db.pending_notifications().items():
        jobs = [dict(r) for r in rows]
        message = format_message(jobs)
        if dry_run:
            log.info("DRY RUN — would send WhatsApp message:\n%s", message)
            continue
        if not settings.whatsapp_target:
            log.error("WHATSAPP_TARGET is not set; cannot send notifications")
            report.errors.append("no_whatsapp_target")
            return
        log.info("Sending WhatsApp notification")
        try:
            sender(settings.whatsapp_target, message)
        except NotificationError as e:
            log.error("%s. Will retry next run.", e)
            report.errors.append("notify_failed")
            continue
        db.mark_notified([r["id"] for r in rows])
        report.messages_sent += 1
        log.info("Notification sent")


def _record_failure(db: Database, settings: Settings, dry_run: bool, sender: Sender) -> None:
    count = int(db.get_state("consecutive_failures", "0")) + 1
    db.set_state("consecutive_failures", str(count))
    if count >= FAILURE_ALERT_THRESHOLD:
        _alert_once(
            db, "failure_alert_sent",
            f"zero2sudo monitor: Instagram check has failed {count} runs in a row. "
            "Check the logs in ~/openclaw-instagram-jobs/logs/.",
            settings, dry_run, sender,
        )


def _alert_once(db: Database, state_key: str, message: str, settings: Settings, dry_run: bool, sender: Sender) -> None:
    """Send an operational alert at most once until the condition clears."""
    if db.get_state(state_key):
        return
    if dry_run or not settings.whatsapp_target:
        log.info("DRY RUN — would send alert:\n%s", message)
    else:
        try:
            sender(settings.whatsapp_target, message)
        except NotificationError as e:
            log.error("Could not send alert: %s", e)
            return
    db.set_state(state_key, "1")


def summarize(r: Report) -> str:
    parts = [
        f"{r.stories_found} Stories",
        f"{r.new_stories} new",
        f"{r.skipped_viewed} already viewed",
        f"{r.classified} classified",
        f"{r.irrelevant} not relevant",
        f"{len(r.new_jobs)} new jobs",
        f"{len(r.new_events)} new events",
        f"{len(r.filtered_out)} filtered out",
        f"{len(r.duplicate_jobs)} duplicates",
        f"{r.messages_sent} messages sent",
    ]
    if r.errors:
        parts.append(f"{len(r.errors)} errors")
    return ", ".join(parts)


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
    file_fmt = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    file = logging.FileHandler(log_dir / "monitor.log")
    file.setFormatter(file_fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers = [stream, file]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check zero2sudo Stories once.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="never send WhatsApp messages")
    mode.add_argument("--live", action="store_true", help="send even if DRY_RUN=true in .env")
    parser.add_argument("--baseline", action="store_true", help="mark current Stories seen without classifying")
    parser.add_argument("--limit", type=int, default=None, help="max new Stories to classify this run")
    args = parser.parse_args(argv)

    settings = load_settings()
    setup_logging(settings.data_dir / "logs")
    dry_run = args.dry_run or (settings.dry_run and not args.live)

    lock_file = open(settings.data_dir / "monitor.lock", "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("Another monitor run is in progress; skipping.")
        return 0

    db = Database(settings.db_path)
    try:
        report = run_cycle(settings, db, dry_run=dry_run, baseline=args.baseline, limit=args.limit)
    finally:
        db.close()
        lock_file.close()
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
