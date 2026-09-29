"""End-to-end cycle tests with Instagram, the LLM, and WhatsApp mocked."""

import pytest

from scripts import instagram
from scripts.database import Database
from scripts.job_extractor import Classification, ExtractionError, Job
from scripts.monitor import FAILURE_ALERT_THRESHOLD, FetchedStory, run_cycle
from scripts.notifier import NotificationError

from scripts.job_extractor import Event

DATADOG = Job(company="Datadog", role="Software Engineer Intern", employment_type="internship",
              season="Summer 2027", location="New York, NY", compensation="$52/hour",
              application_url="https://careers.datadoghq.com/detail/123/", category="software_engineering",
              countries=["United States"], min_hourly_usd=52)


class FakeInstagram:
    def __init__(self, tmp_path, keys):
        self.tmp_path = tmp_path
        self.keys = list(keys)
        self.viewed = set()
        self.error = None

    def __call__(self, settings, db):
        if self.error:
            raise self.error
        out = []
        for k in self.keys:
            path = self.tmp_path / f"{k}.jpg"
            path.write_bytes(k.encode())
            out.append(FetchedStory(instagram.Story(k, int(k), 2, "https://cdn/x.jpg", None, k in self.viewed), path))
        return out


class FakeLLM:
    def __init__(self, results):
        self.results = results  # story_key -> Classification | Exception
        self.calls = []

    def __call__(self, image_path, link_url, model, filtering=None, username=None):
        key = image_path.stem
        self.calls.append(key)
        result = self.results[key]
        if isinstance(result, Exception):
            raise result
        return result


class FakeWhatsApp:
    def __init__(self):
        self.sent = []
        self.fail = False

    def __call__(self, target, message):
        if self.fail:
            raise NotificationError("offline")
        self.sent.append(message)
        return "msg-id"


JOB = Classification(True, "software_engineering", 0.95, [DATADOG])
NOT_JOB = Classification(False, "irrelevant", 0.9, [])


@pytest.fixture
def fakes(tmp_path):
    return FakeInstagram(tmp_path, ["1", "2"]), FakeLLM({"1": JOB, "2": NOT_JOB}), FakeWhatsApp()


def cycle(settings, db, fakes, **kw):
    ig, llm, wa = fakes
    return run_cycle(settings, db, dry_run=kw.pop("dry_run", False), fetcher=ig, classifier=llm, sender=wa, **kw)


def test_full_cycle_sends_one_notification(settings, db, fakes):
    r = cycle(settings, db, fakes)
    assert (r.stories_found, r.new_stories, r.classified, r.irrelevant) == (2, 2, 2, 1)
    assert r.new_jobs == ["Datadog - Software Engineer Intern"]
    assert len(fakes[2].sent) == 1
    assert fakes[2].sent[0].startswith("NEW JOB\n\nDatadog\nSoftware Engineer Intern")


def test_second_run_does_not_call_llm_or_resend(settings, db, fakes):
    cycle(settings, db, fakes)
    r = cycle(settings, db, fakes)
    assert r.new_stories == 0 and r.already_processed == 2
    assert fakes[1].calls == ["1", "2"]  # no new LLM calls
    assert len(fakes[2].sent) == 1


def test_restart_recovery_does_not_resend(settings, fakes):
    db1 = Database(settings.db_path)
    cycle(settings, db1, fakes)
    db1.close()
    db2 = Database(settings.db_path)  # new process after a restart
    r = cycle(settings, db2, fakes)
    db2.close()
    assert r.new_stories == 0 and len(fakes[2].sent) == 1


def test_same_job_in_two_stories_notifies_once(settings, db, fakes):
    ig, llm, wa = fakes
    llm.results["2"] = JOB
    r = cycle(settings, db, fakes)
    assert r.duplicate_jobs == ["Datadog - Software Engineer Intern"]
    assert len(wa.sent) == 1


def test_dry_run_updates_db_but_sends_nothing(settings, db, fakes):
    r = cycle(settings, db, fakes, dry_run=True)
    assert fakes[2].sent == [] and r.new_jobs
    assert not db.needs_processing("1")
    assert len(db.pending_notifications()) == 1  # still unsent, truthfully


def test_whatsapp_failure_retries_without_duplicate_job(settings, db, fakes):
    fakes[2].fail = True
    r = cycle(settings, db, fakes)
    assert "notify_failed" in r.errors
    fakes[2].fail = False
    cycle(settings, db, fakes)
    assert len(fakes[2].sent) == 1
    assert db.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_llm_failure_records_story_and_retries(settings, db, fakes):
    fakes[1].results["1"] = ExtractionError("timeout")
    r = cycle(settings, db, fakes)
    assert fakes[2].sent == [] and db.needs_processing("1")
    assert db.get_story("1")["attempts"] == 1
    fakes[1].results["1"] = JOB
    cycle(settings, db, fakes)
    assert len(fakes[2].sent) == 1


def test_auth_expired_alerts_once(settings, db, fakes):
    fakes[0].error = instagram.AuthExpiredError("expired")
    cycle(settings, db, fakes)
    cycle(settings, db, fakes)
    assert len(fakes[2].sent) == 1
    assert "authentication has expired" in fakes[2].sent[0]


def test_routine_failure_alerts_only_after_threshold(settings, db, fakes):
    fakes[0].error = RuntimeError("network down")
    for _ in range(FAILURE_ALERT_THRESHOLD - 1):
        cycle(settings, db, fakes)
    assert fakes[2].sent == []
    cycle(settings, db, fakes)
    cycle(settings, db, fakes)
    assert len(fakes[2].sent) == 1


def test_baseline_marks_seen_without_llm(settings, db, fakes):
    r = cycle(settings, db, fakes, baseline=True)
    assert fakes[1].calls == [] and fakes[2].sent == []
    assert r.new_stories == 2
    assert not db.needs_processing("1")


def test_limit_caps_llm_calls(settings, db, fakes):
    cycle(settings, db, fakes, limit=1)
    assert fakes[1].calls == ["1"]
    assert db.needs_processing("2")


def test_multiple_jobs_in_one_story_single_message(settings, db, fakes):
    anthropic = Job(company="Anthropic", role="Software Engineer Intern", employment_type="internship",
                    location="San Francisco, CA", category="software_engineering",
                    application_url=DATADOG.application_url)  # same Story link
    fakes[1].results["1"] = Classification(True, "software_engineering", 0.9, [DATADOG, anthropic])
    cycle(settings, db, fakes)
    assert len(fakes[2].sent) == 1
    msg = fakes[2].sent[0]
    assert msg.startswith("NEW JOBS") and "1. Datadog" in msg and "2. Anthropic" in msg
    assert "Compensation: Not listed" in msg


def test_already_viewed_stories_are_never_analyzed(settings, db, fakes):
    ig, llm, wa = fakes
    ig.viewed = {"1"}
    r = cycle(settings, db, fakes)
    assert llm.calls == ["2"]
    assert r.skipped_viewed == 1 and r.new_stories == 1
    assert not db.needs_processing("1")
    assert wa.sent == []  # the job Story was viewed, so nothing to send
    cycle(settings, db, fakes)
    assert llm.calls == ["2"]


def test_per_job_filtering_in_one_story(settings, db, fakes):
    design = Job(company="Coinbase", role="Product Design Intern", employment_type="internship", category="other")
    swe = Job(company="Coinbase", role="Software Engineer Intern", employment_type="internship",
              category="software_engineering")
    fakes[1].results["1"] = Classification(True, "software_engineering", 0.9, [swe, design])
    r = cycle(settings, db, fakes)
    assert r.new_jobs == ["Coinbase - Software Engineer Intern"]
    assert r.filtered_out == ["Coinbase - Product Design Intern (category other)"]


def test_canada_and_low_pay_jobs_are_not_sent(settings, db, fakes):
    canada = Job(company="Arc'teryx", role="Data Co-op", employment_type="internship",
                 category="data_engineering", countries=["Canada"])
    cheap = Job(company="Cheapco", role="SWE Intern", employment_type="internship",
                category="software_engineering", min_hourly_usd=20)
    fakes[1].results["1"] = Classification(True, "data_engineering", 0.9, [canada])
    fakes[1].results["2"] = Classification(True, "software_engineering", 0.9, [cheap])
    r = cycle(settings, db, fakes)
    assert r.new_jobs == [] and len(r.filtered_out) == 2
    assert fakes[2].sent == []


def test_event_story_sends_event_message(settings, db, fakes):
    event = Event(organizer="Jane Street", name="Info Session", date_time="Oct 3",
                  registration_url="https://janestreet.com/register")
    fakes[1].results["2"] = Classification(False, "irrelevant", 0.9, [], True, [event])
    r = cycle(settings, db, fakes)
    assert r.new_events == ["Jane Street - Info Session"]
    events = [m for m in fakes[2].sent if m.startswith("NEW EVENT")]
    assert len(events) == 1 and "https://janestreet.com/register" in events[0]
