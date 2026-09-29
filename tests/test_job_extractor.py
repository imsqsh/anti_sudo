import json
import subprocess

import pytest

from scripts import job_extractor
from scripts.job_extractor import ExtractionError, classify_story, event_rejection, job_rejection, parse_response
from scripts.notifier import format_message

FILTERING = {
    "categories": ["software_engineering", "machine_learning", "data_engineering", "research"],
    "employment_types": ["internship", "new_grad"],
    "exclude_countries": ["Canada"],
    "min_hourly_usd": 30,
    "education": {"degree_levels": ["bachelors", "masters"], "majors": ["Computer Science", "Data Science"]},
    "send_events": True,
}
LINK = "https://careers.datadoghq.com/detail/123/"


def response(is_job=True, relevance="software_engineering", jobs=None, confidence=0.95, events=None):
    return json.dumps({"is_job": is_job, "is_event": bool(events), "relevance": relevance,
                       "confidence": confidence, "jobs": jobs or [], "events": events or []})


DATADOG = {
    "company": "Datadog", "role": "Software Engineer Intern", "employment_type": "internship",
    "season": "Summer 2027", "location": "New York, NY", "compensation": "$52/hour",
    "application_url": LINK, "additional_details": None, "category": "software_engineering",
    "countries": ["United States"], "min_hourly_usd": 52, "masters_eligible": None, "major_eligible": None,
}

EVENT = {
    "organizer": "Jane Street", "name": "Women in Tech Info Session", "event_type": "Info session",
    "date_time": "Oct 3, 6pm ET", "location": "Virtual", "registration_url": None, "details": None,
}


def job(**overrides):
    return parse_response(response(jobs=[{**DATADOG, **overrides}]), LINK).jobs[0]


def test_job_classification():
    c = parse_response(response(jobs=[DATADOG]), LINK)
    assert c.is_job and c.relevance == "software_engineering" and c.confidence == 0.95
    assert c.jobs[0].company == "Datadog" and c.jobs[0].countries == ["United States"]
    assert job_rejection(c.jobs[0], FILTERING) is None


def test_irrelevant_story_classification():
    c = parse_response(response(is_job=False, relevance="irrelevant"))
    assert not c.is_job and c.jobs == [] and c.events == []


def test_non_technical_job_is_filtered_out():
    assert job_rejection(job(role="Product Design Intern", category="other"), FILTERING) == "category other"


def test_full_time_only_job_is_filtered_out():
    assert "employment type" in job_rejection(job(employment_type="full_time"), FILTERING)


def test_preferred_location_never_rejects():
    assert job_rejection(job(location="Austin, TX"), FILTERING) is None


def test_canada_only_job_is_filtered_out():
    assert "Canada" in job_rejection(job(location="Vancouver, BC", countries=["Canada"]), FILTERING)


def test_us_and_canada_job_is_kept():
    assert job_rejection(job(countries=["United States", "Canada"]), FILTERING) is None


def test_low_pay_is_filtered_out():
    assert "below" in job_rejection(job(compensation="$25/hr", min_hourly_usd=25), FILTERING)


def test_unlisted_pay_is_kept():
    assert job_rejection(job(compensation=None, min_hourly_usd=None), FILTERING) is None


def test_exactly_30_per_hour_is_kept():
    assert job_rejection(job(min_hourly_usd=30), FILTERING) is None


def test_masters_excluded_is_filtered_out():
    assert job_rejection(job(masters_eligible=False), FILTERING) == "excludes Master's candidates"


def test_masters_unstated_is_kept():
    assert job_rejection(job(masters_eligible=None), FILTERING) is None


def test_wrong_major_is_filtered_out():
    assert job_rejection(job(major_eligible=False), FILTERING) == "requires majors outside the profile"


def test_event_extraction():
    c = parse_response(response(is_job=False, relevance="irrelevant", events=[EVENT]), LINK)
    assert c.is_event and not c.is_job
    assert c.events[0].organizer == "Jane Street"
    assert c.events[0].registration_url == LINK  # Story link is the registration link
    assert event_rejection(c.events[0], FILTERING) is None


def test_event_without_link_keeps_null_url():
    c = parse_response(response(is_job=False, relevance="irrelevant", events=[EVENT]), None)
    assert c.events[0].registration_url is None
    assert "Register:\nNot listed" in format_message([{"kind": "event", "company": "Jane Street",
                                                      "role": "Info Session", "application_url": None}])


def test_prompt_includes_profile_majors():
    from scripts.job_extractor import build_prompt
    prompt = build_prompt(None, FILTERING)
    assert "Data Science" in prompt and "masters" in prompt


def test_job_extraction_with_code_fences_and_prose():
    text = "Here you go:\n```json\n" + response(jobs=[DATADOG]) + "\n```"
    assert parse_response(text, LINK).jobs[0].role == "Software Engineer Intern"


def test_missing_compensation():
    c = parse_response(response(jobs=[{**DATADOG, "compensation": None}]))
    assert c.jobs[0].compensation is None
    assert "Compensation: Not listed" in format_message([vars(c.jobs[0])])


def test_missing_location():
    c = parse_response(response(jobs=[{**DATADOG, "location": "null"}]))
    assert c.jobs[0].location is None
    assert "Location: Not listed" in format_message([vars(c.jobs[0])])


def test_missing_url():
    c = parse_response(response(jobs=[{**DATADOG, "application_url": None}]), link_url=None)
    assert c.jobs[0].application_url is None
    assert "Apply:\nNot listed" in format_message([vars(c.jobs[0])])


def test_story_link_wins_over_model_url():
    c = parse_response(response(jobs=[{**DATADOG, "application_url": "https://made-up.example"}]), LINK)
    assert c.jobs[0].application_url == LINK


def test_non_http_url_from_model_is_dropped():
    c = parse_response(response(jobs=[{**DATADOG, "application_url": "datadog careers page"}]), None)
    assert c.jobs[0].application_url is None


def test_multiple_jobs_extracted_separately():
    other = {**DATADOG, "company": "Anthropic", "role": "Software Engineer Intern", "location": "San Francisco, CA"}
    c = parse_response(response(jobs=[DATADOG, other]))
    assert [j.company for j in c.jobs] == ["Datadog", "Anthropic"]


@pytest.mark.parametrize("bad", ["", "not json", '{"relevance": "x"}', response(jobs=[])])
def test_malformed_output_raises(bad):
    with pytest.raises(ExtractionError):
        parse_response(bad)


def test_unknown_relevance_is_normalized():
    assert parse_response(response(relevance="banana", jobs=[DATADOG])).relevance == "other"


def test_classify_story_calls_openclaw_infer(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        out = "[log line]\n" + json.dumps({"ok": True, "outputs": [{"text": response(jobs=[DATADOG])}]})
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(job_extractor.subprocess, "run", fake_run)
    image = tmp_path / "s.jpg"
    image.write_bytes(b"x")
    c = classify_story(image, LINK, "anthropic/claude-opus-5-5")

    assert c.jobs[0].company == "Datadog"
    cmd = captured["cmd"]
    assert cmd[:4] == ["openclaw", "infer", "model", "run"]
    assert cmd[cmd.index("--model") + 1] == "anthropic/claude-opus-5-5"
    assert LINK in cmd[cmd.index("--prompt") + 1]


def test_classify_story_raises_on_cli_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(
        job_extractor.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout='{"ok": false, "error": "rate limited"}', stderr=""),
    )
    with pytest.raises(ExtractionError, match="rate limited"):
        classify_story(tmp_path / "s.jpg", None, "m")


def test_canada_only_event_is_filtered_out():
    c = parse_response(response(is_job=False, relevance="irrelevant", events=[{**EVENT, "countries": ["Canada"]}]))
    assert "Canada" in event_rejection(c.events[0], FILTERING)
