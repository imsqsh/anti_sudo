"""WhatsApp message formatting and delivery through `openclaw message send`."""

from __future__ import annotations

import json
import subprocess
from typing import Mapping, Sequence

NOT_LISTED = "Not listed"

_EMPLOYMENT_LABELS = {
    "internship": "Internship",
    "new_grad": "New Grad",
    "full_time": "Full-time",
    "other": None,
}


class NotificationError(RuntimeError):
    pass


def _v(value) -> str:
    return value if value else NOT_LISTED


def job_type(job: Mapping) -> str:
    label = _EMPLOYMENT_LABELS.get(job.get("employment_type") or "")
    parts = [p for p in (job.get("season"), label) if p]
    # Avoid "Summer 2027 Internship Internship" when the season already names the type.
    if len(parts) == 2 and parts[1].lower() in parts[0].lower():
        parts = parts[:1]
    return " ".join(parts) if parts else NOT_LISTED


def format_message(items: Sequence[Mapping], source: str) -> str:
    """Format one Story's jobs, or one Story's events (rows with kind='event')."""
    if items and items[0].get("kind") == "event":
        return format_events(items, source)
    return format_jobs(items, source)


def format_events(events: Sequence[Mapping], source: str) -> str:
    if len(events) == 1:
        e = events[0]
        return "\n".join([
            "NEW EVENT",
            "",
            _v(e.get("company")),
            _v(e.get("role")),
            "",
            f"Type: {_v(e.get('season'))}",
            f"When: {_v(e.get('event_time'))}",
            f"Where: {_v(e.get('location'))}",
            "",
            "Register:",
            _v(e.get("application_url")),
            "",
            f"Source: {source}",
        ])

    blocks = ["NEW EVENTS"]
    for i, e in enumerate(events, 1):
        blocks.append("\n".join([
            f"{i}. {_v(e.get('company'))}",
            _v(e.get("role")),
            f"When: {_v(e.get('event_time'))}",
            f"Where: {_v(e.get('location'))}",
            e.get("application_url") or f"Register: {NOT_LISTED}",
        ]))
    blocks.append(f"Source: {source}")
    return "\n\n".join(blocks)


def format_jobs(jobs: Sequence[Mapping], source: str) -> str:
    if len(jobs) == 1:
        j = jobs[0]
        return "\n".join([
            "NEW JOB",
            "",
            _v(j.get("company")),
            _v(j.get("role")),
            "",
            f"Type: {job_type(j)}",
            f"Location: {_v(j.get('location'))}",
            f"Compensation: {_v(j.get('compensation'))}",
            "",
            "Apply:",
            _v(j.get("application_url")),
            "",
            f"Source: {source}",
        ])

    blocks = ["NEW JOBS"]
    for i, j in enumerate(jobs, 1):
        # Compact format: bare values, but label a field when it's missing so it's never ambiguous.
        blocks.append("\n".join([
            f"{i}. {_v(j.get('company'))}",
            _v(j.get("role")),
            j.get("location") or f"Location: {NOT_LISTED}",
            j.get("compensation") or f"Compensation: {NOT_LISTED}",
            j.get("application_url") or f"Apply: {NOT_LISTED}",
        ]))
    blocks.append(f"Source: {source}")
    return "\n\n".join(blocks)


def send_whatsapp(target: str, message: str, timeout: int = 120) -> str:
    """Send via OpenClaw's WhatsApp channel. Returns the message id; raises NotificationError."""
    cmd = [
        "openclaw", "message", "send",
        "--channel", "whatsapp",
        "--target", target,
        "--message", message,
        "--json",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        raise NotificationError(f"openclaw message send failed: {e}") from e

    start = proc.stdout.find("{")
    try:
        result = json.loads(proc.stdout[start:]) if start != -1 else {}
    except json.JSONDecodeError:
        result = {}

    message_id = result.get("messageId")
    if proc.returncode != 0 or result.get("error") or not message_id:
        err = result.get("error") or proc.stderr.strip()[-300:] or "no messageId returned"
        raise NotificationError(f"WhatsApp delivery failed: {err}")
    return message_id
