"""SQLite persistence: seen Stories, deduplicated jobs, and small bits of monitor state."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from scripts.job_extractor import Event, Job

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "database" / "schema.sql"
MAX_ATTEMPTS = 3


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm_text(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _norm_url(url: str | None) -> str | None:
    if not url:
        return None
    p = urlparse(url.strip())
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme.lower(), p.netloc.lower().removeprefix("www."), path, "", p.query, ""))


def job_keys(job: Job, use_url: bool = True) -> tuple[str, str | None]:
    """(dedup_key, url_key). A job is a duplicate if either key has been seen before.

    use_url=False when the Story lists several jobs: they all share the Story's single link,
    so the URL says nothing about which job it is.
    """
    company = _norm_text(job.company)
    dedup_key = f"job|{company}|{_norm_text(job.role)}"
    url = _norm_url(job.application_url) if use_url else None
    return dedup_key, (f"job|{company}|{url}" if url else None)


def event_keys(event: Event, use_url: bool = True) -> tuple[str, str | None]:
    organizer = _norm_text(event.organizer)
    dedup_key = f"event|{organizer}|{_norm_text(event.name)}|{_norm_text(event.date_time)}"
    url = _norm_url(event.registration_url) if use_url else None
    return dedup_key, (f"event|{organizer}|{url}" if url else None)


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA_PATH.read_text())
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # --- stories -----------------------------------------------------------

    def record_story(self, story_key: str, taken_at: int | None, source_url: str | None) -> bool:
        """Insert a Story if unseen. Returns True if it was new."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO stories (story_key, first_seen_at, taken_at, source_url) VALUES (?, ?, ?, ?)",
            (story_key, now_iso(), taken_at, source_url),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def get_story(self, story_key: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM stories WHERE story_key = ?", (story_key,)).fetchone()

    def needs_processing(self, story_key: str) -> bool:
        row = self.get_story(story_key)
        return row is not None and not row["processed"]

    def mark_classified(
        self, story_key: str, *, is_job: bool, relevance: str, confidence: float,
        raw: str | None = None, content_hash: str | None = None,
    ) -> None:
        self.conn.execute(
            """UPDATE stories SET is_job = ?, relevance = ?, confidence = ?, text_content = ?,
               content_hash = ?, processed = 1, last_error = NULL WHERE story_key = ?""",
            (int(is_job), relevance, confidence, raw, content_hash, story_key),
        )
        self.conn.commit()

    def mark_failed(self, story_key: str, error: str) -> bool:
        """Record a failed attempt. Returns True if we've now given up on this Story."""
        self.conn.execute(
            "UPDATE stories SET attempts = attempts + 1, last_error = ? WHERE story_key = ?",
            (error[:500], story_key),
        )
        row = self.get_story(story_key)
        gave_up = row["attempts"] >= MAX_ATTEMPTS
        if gave_up:
            self.conn.execute("UPDATE stories SET processed = 1 WHERE story_key = ?", (story_key,))
        self.conn.commit()
        return gave_up

    def mark_skipped(self, story_key: str, reason: str) -> None:
        """Mark a Story processed without classifying it (backlog baseline, already viewed, ...)."""
        self.conn.execute(
            "UPDATE stories SET processed = 1, last_error = ? WHERE story_key = ?",
            (f"skipped: {reason}", story_key),
        )
        self.conn.commit()

    # --- jobs --------------------------------------------------------------

    def add_job(self, job: Job, story_key: str, use_url_key: bool = True) -> int | None:
        """Insert a job unless it's a duplicate. Returns the new row id, or None if duplicate."""
        dedup_key, url_key = job_keys(job, use_url_key)
        return self._insert(
            story_key, dedup_key, url_key, kind="job", company=job.company, role=job.role,
            employment_type=job.employment_type, season=job.season, location=job.location,
            compensation=job.compensation, application_url=job.application_url,
            additional_details=job.additional_details,
        )

    def add_event(self, event: Event, story_key: str, use_url_key: bool = True) -> int | None:
        """Insert an event unless it's a duplicate. Returns the new row id, or None if duplicate."""
        dedup_key, url_key = event_keys(event, use_url_key)
        return self._insert(
            story_key, dedup_key, url_key, kind="event", company=event.organizer, role=event.name,
            season=event.event_type, event_time=event.date_time, location=event.location,
            application_url=event.registration_url, additional_details=event.details,
        )

    def _insert(self, story_key: str, dedup_key: str, url_key: str | None, **fields) -> int | None:
        exists = self.conn.execute(
            "SELECT 1 FROM jobs WHERE dedup_key = ? OR (url_key IS NOT NULL AND url_key = ?)",
            (dedup_key, url_key),
        ).fetchone()
        if exists:
            return None
        story = self.get_story(story_key)
        fields.update(dedup_key=dedup_key, url_key=url_key, first_seen_at=now_iso(),
                      story_id=story["id"] if story else None)
        cols = ", ".join(fields)
        cur = self.conn.execute(
            f"INSERT INTO jobs ({cols}) VALUES ({', '.join('?' * len(fields))})", tuple(fields.values())
        )
        self.conn.commit()
        return cur.lastrowid

    def pending_notifications(self) -> dict[tuple[int | None, str], list[sqlite3.Row]]:
        """Unsent items grouped by (story_id, kind): one WhatsApp message per Story per kind."""
        rows = self.conn.execute(
            "SELECT * FROM jobs WHERE notification_sent = 0 ORDER BY story_id, kind DESC, id"
        ).fetchall()
        groups: dict[tuple[int | None, str], list[sqlite3.Row]] = {}
        for row in rows:
            groups.setdefault((row["story_id"], row["kind"]), []).append(row)
        return groups

    def mark_notified(self, job_ids: list[int]) -> None:
        ts = now_iso()
        self.conn.executemany(
            "UPDATE jobs SET notification_sent = 1, notified_at = ? WHERE id = ?",
            [(ts, i) for i in job_ids],
        )
        self.conn.execute(
            """UPDATE stories SET notification_sent = 1 WHERE id IN
               (SELECT story_id FROM jobs WHERE id IN (%s))""" % ",".join("?" * len(job_ids)),
            job_ids,
        )
        self.conn.commit()

    # --- state -------------------------------------------------------------

    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str | None) -> None:
        self.conn.execute(
            "INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()
