"""LLM classification + extraction for a single Story, via `openclaw infer`.

One model call per new Story. The model only reports facts it can see; the personal
filtering rules (category, Canada, pay floor, education) are applied in code afterwards.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

RELEVANCE_CATEGORIES = {
    "software_engineering",
    "machine_learning",
    "artificial_intelligence",
    "data_engineering",
    "systems",
    "infrastructure",
    "cybersecurity",
    "research",
    "other",
    "irrelevant",
}

EMPLOYMENT_TYPES = {"internship", "new_grad", "full_time", "other"}

PROMPT_TEMPLATE = """\
You are reading one Instagram Story image from the account "{username}", which posts \
tech internship / new-grad job openings and tech recruiting events, mixed with unrelated content.

Link sticker URL attached to this Story (may be null): {link_url}

Candidate profile (only needed for the eligibility fields below):
- Degree levels: {degree_levels}
- Majors: {majors}

Identify:
1. JOBS: specific open positions someone can apply to.
2. EVENTS: recruiting or tech events someone can register for or attend (info sessions, \
hackathons, conferences, career fairs, workshops, networking events, fellowship/program \
application events).
Interview advice, Q&A, memes, cars, travel, personal updates, polls, and general promotion \
are neither.

Rules:
- Only use information visible in the image or the link URL. Never guess. Use null when not shown.
- URLs: use the link sticker URL above if present; otherwise a full URL only if clearly printed \
in the image; otherwise null. Never construct a URL.
- One entry per job and per event.

Per job:
- category: software_engineering, machine_learning, artificial_intelligence, data_engineering \
(incl. data science/analytics engineering), systems (incl. hardware/embedded/ASIC), \
infrastructure, cybersecurity, research (technical or quant research), or other \
(non-technical: product management, design, user research, business, marketing, sales, ...).
- employment_type: internship, new_grad, full_time, or other (co-ops count as internship).
- countries: countries of the listed locations, e.g. ["United States"], ["Canada"], \
["United States", "Canada"]; [] if no location is shown. "Remote" alone -> [].
- min_hourly_usd: the lowest listed pay as USD per hour (annual salary / 2080; monthly * 12 / 2080); \
null if pay is not shown.
- masters_eligible: false ONLY if the posting explicitly excludes Master's students \
(e.g. "undergraduates only", "Bachelor's candidates only"); true if it explicitly allows them; \
otherwise null.
- major_eligible: false ONLY if the posting explicitly lists required majors and none of the \
candidate's majors (or close equivalents) are among them; true if one is listed; otherwise null.

Respond with ONLY this JSON object, no prose, no code fences:
{{"is_job": bool, "is_event": bool, "relevance": "<dominant job category, or irrelevant>",
  "confidence": <0.0-1.0>,
  "jobs": [{{"company": str|null, "role": str|null, "category": str, "employment_type": str|null,
    "season": str|null, "location": str|null, "countries": [str], "compensation": str|null,
    "min_hourly_usd": number|null, "masters_eligible": bool|null, "major_eligible": bool|null,
    "application_url": str|null, "additional_details": str|null}}],
  "events": [{{"organizer": str|null, "name": str|null, "event_type": str|null,
    "date_time": str|null, "location": str|null, "countries": [str], "registration_url": str|null,
    "details": str|null}}]}}
Event countries: same rules as job countries (e.g. ["Canada"] for a Canada-only event).
season: the term if shown, e.g. "Summer 2027". compensation: the pay text as shown. \
additional_details / details: at most one short line, or null.
"""


class ExtractionError(RuntimeError):
    """The LLM call failed or returned unusable output. The Story should be retried later."""


@dataclass
class Job:
    company: str | None
    role: str | None
    employment_type: str | None = None
    season: str | None = None
    location: str | None = None
    compensation: str | None = None
    application_url: str | None = None
    additional_details: str | None = None
    category: str | None = None
    countries: list[str] = field(default_factory=list)
    min_hourly_usd: float | None = None
    masters_eligible: bool | None = None
    major_eligible: bool | None = None


@dataclass
class Event:
    organizer: str | None
    name: str | None
    event_type: str | None = None
    date_time: str | None = None
    location: str | None = None
    registration_url: str | None = None
    details: str | None = None
    countries: list[str] = field(default_factory=list)


@dataclass
class Classification:
    is_job: bool
    relevance: str
    confidence: float
    jobs: list[Job] = field(default_factory=list)
    is_event: bool = False
    events: list[Event] = field(default_factory=list)


def build_prompt(link_url: str | None, filtering: dict | None = None, username: str = "the account") -> str:
    education = (filtering or {}).get("education") or {}
    return PROMPT_TEMPLATE.format(
        username=username,
        link_url=link_url or "null",
        degree_levels=", ".join(education.get("degree_levels") or ["bachelors", "masters"]),
        majors=", ".join(education.get("majors") or ["Computer Science"]),
    )


def _clean(value) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value if value and value.lower() not in ("null", "none", "n/a", "unknown") else None


def _bool_or_none(value) -> bool | None:
    return value if isinstance(value, bool) else None


def _float_or_none(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _countries(value) -> list[str]:
    return [c for c in (_clean(c) for c in value) if c] if isinstance(value, list) else []


def _url(raw, link_url: str | None) -> str | None:
    """Prefer the Story's own link; otherwise accept only a real http(s) URL from the model."""
    if link_url:
        return link_url
    url = _clean(raw)
    return url if url and url.startswith(("http://", "https://")) else None


def parse_response(text: str, link_url: str | None = None) -> Classification:
    """Parse and validate the model's JSON. Raises ExtractionError on anything malformed."""
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ExtractionError("No JSON object in model output")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as e:
        raise ExtractionError(f"Invalid JSON from model: {e}") from e

    if not isinstance(data, dict) or not isinstance(data.get("is_job"), bool):
        raise ExtractionError("Model output missing boolean is_job")
    is_event = data.get("is_event") is True

    relevance = data.get("relevance")
    if relevance not in RELEVANCE_CATEGORIES:
        relevance = "other" if data["is_job"] else "irrelevant"

    confidence = _float_or_none(data.get("confidence")) or 0.0

    jobs: list[Job] = []
    if data["is_job"]:
        for raw in data.get("jobs") or []:
            if not isinstance(raw, dict):
                continue
            employment_type = _clean(raw.get("employment_type"))
            category = _clean(raw.get("category"))
            countries = raw.get("countries")
            job = Job(
                company=_clean(raw.get("company")),
                role=_clean(raw.get("role")),
                employment_type=employment_type if employment_type in EMPLOYMENT_TYPES else None,
                season=_clean(raw.get("season")),
                location=_clean(raw.get("location")),
                compensation=_clean(raw.get("compensation")),
                application_url=_url(raw.get("application_url"), link_url),
                additional_details=_clean(raw.get("additional_details")),
                category=category if category in RELEVANCE_CATEGORIES else relevance,
                countries=_countries(countries),
                min_hourly_usd=_float_or_none(raw.get("min_hourly_usd")),
                masters_eligible=_bool_or_none(raw.get("masters_eligible")),
                major_eligible=_bool_or_none(raw.get("major_eligible")),
            )
            if job.company or job.role:
                jobs.append(job)

    events: list[Event] = []
    if is_event:
        for raw in data.get("events") or []:
            if not isinstance(raw, dict):
                continue
            event = Event(
                organizer=_clean(raw.get("organizer")),
                name=_clean(raw.get("name")),
                event_type=_clean(raw.get("event_type")),
                date_time=_clean(raw.get("date_time")),
                location=_clean(raw.get("location")),
                registration_url=_url(raw.get("registration_url"), link_url),
                details=_clean(raw.get("details")),
                countries=_countries(raw.get("countries")),
            )
            if event.organizer or event.name:
                events.append(event)

    if data["is_job"] and not jobs and not events:
        raise ExtractionError("Model said is_job but returned no usable jobs")
    if is_event and not events and not jobs:
        raise ExtractionError("Model said is_event but returned no usable events")

    return Classification(
        is_job=data["is_job"] and bool(jobs), relevance=relevance, confidence=confidence,
        jobs=jobs, is_event=bool(events), events=events,
    )


def classify_story(
    image_path: Path, link_url: str | None, model: str, filtering: dict | None = None,
    username: str = "the account", timeout: int = 180,
) -> Classification:
    """Run one `openclaw infer model run` call on the Story image."""
    cmd = [
        "openclaw", "infer", "model", "run",
        "--model", model,
        "--file", str(image_path),
        "--prompt", build_prompt(link_url, filtering, username),
        "--json",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        raise ExtractionError(f"openclaw infer failed: {e}") from e

    payload = _parse_cli_json(proc.stdout)
    if proc.returncode != 0 or not payload or not payload.get("ok"):
        err = (payload or {}).get("error") or proc.stderr.strip()[-300:]
        raise ExtractionError(f"openclaw infer error: {err}")

    text = "".join(o.get("text") or "" for o in payload.get("outputs") or [])
    return parse_response(text, link_url)


def _parse_cli_json(stdout: str) -> dict | None:
    """openclaw may print log lines before the JSON; take the JSON object that follows them."""
    start = stdout.find("{\n")
    if start == -1:
        start = stdout.find("{")
    if start == -1:
        return None
    try:
        return json.loads(stdout[start:])
    except json.JSONDecodeError:
        return None


# --- personal filtering ------------------------------------------------------


def job_rejection(job: Job, filtering: dict) -> str | None:
    """Why this job should NOT be sent, or None if it passes. Location preferences never reject."""
    categories = filtering.get("categories") or []
    if job.category not in categories:
        return f"category {job.category}"

    types = filtering.get("employment_types") or []
    if job.employment_type is not None and job.employment_type not in types:
        return f"employment type {job.employment_type}"

    if _only_in_excluded_countries(job.countries, filtering):
        return f"located only in {', '.join(job.countries)}"

    floor = filtering.get("min_hourly_usd")
    if floor is not None and job.min_hourly_usd is not None and job.min_hourly_usd < floor:
        return f"pay ${job.min_hourly_usd:.0f}/hr below ${floor}/hr"

    levels = [l.lower() for l in (filtering.get("education") or {}).get("degree_levels") or []]
    if "masters" in levels and job.masters_eligible is False:
        return "excludes Master's candidates"

    if job.major_eligible is False:
        return "requires majors outside the profile"

    return None


def event_rejection(event: Event, filtering: dict) -> str | None:
    if not filtering.get("send_events", True):
        return "events disabled"
    if _only_in_excluded_countries(event.countries, filtering):
        return f"located only in {', '.join(event.countries)}"
    return None


def _only_in_excluded_countries(countries: list[str], filtering: dict) -> bool:
    excluded = {c.lower() for c in filtering.get("exclude_countries") or []}
    return bool(countries) and all(c.lower() in excluded for c in countries)
