from scripts.database import MAX_ATTEMPTS, Database, job_keys
from scripts.job_extractor import Job

DATADOG = Job(company="Datadog", role="Software Engineer Intern", location="New York, NY",
              application_url="https://careers.datadoghq.com/detail/123/")


def test_duplicate_job_detection_same_company_and_role(db):
    db.record_story("1", 1, None)
    db.record_story("2", 2, None)
    assert db.add_job(DATADOG, "1") is not None
    reworded = Job(company="DATADOG", role="Software Engineer Intern!", application_url=None)
    assert db.add_job(reworded, "2") is None


def test_duplicate_job_detection_same_url_different_wording(db):
    db.record_story("1", 1, None)
    db.add_job(DATADOG, "1")
    same_url = Job(company="Datadog", role="SWE Intern (Summer 2027)",
                   application_url="https://www.careers.datadoghq.com/detail/123")
    assert db.add_job(same_url, "1") is None


def test_different_jobs_are_not_duplicates(db):
    db.record_story("1", 1, None)
    db.add_job(DATADOG, "1")
    other = Job(company="Datadog", role="Data Engineer Intern", application_url="https://careers.datadoghq.com/detail/456/")
    assert db.add_job(other, "1") is not None


def test_job_keys_normalize():
    a = job_keys(Job(company="Meta ", role="Data Engineer Intern", application_url="https://www.metacareers.com/j/1/"))
    b = job_keys(Job(company="meta", role="data engineer intern", application_url="https://metacareers.com/j/1"))
    assert a == b


def test_database_persistence_across_restart(settings):
    first = Database(settings.db_path)
    first.record_story("1", 1, "https://x")
    first.mark_classified("1", is_job=True, relevance="software_engineering", confidence=0.9)
    job_id = first.add_job(DATADOG, "1")
    first.mark_notified([job_id])
    first.close()

    second = Database(settings.db_path)  # simulates OpenClaw/machine restart
    assert second.record_story("1", 1, "https://x") is False
    assert not second.needs_processing("1")
    assert second.add_job(DATADOG, "1") is None
    assert second.pending_notifications() == {}
    second.close()


def test_failed_story_retries_then_gives_up(db):
    db.record_story("1", 1, None)
    for attempt in range(1, MAX_ATTEMPTS):
        assert db.mark_failed("1", "boom") is False
        assert db.needs_processing("1")
    assert db.mark_failed("1", "boom") is True
    assert not db.needs_processing("1")


def test_pending_notifications_grouped_by_story(db):
    db.record_story("1", 1, None)
    db.record_story("2", 2, None)
    db.add_job(DATADOG, "1")
    db.add_job(Job(company="Anthropic", role="SWE Intern"), "1")
    db.add_job(Job(company="Meta", role="DE Intern"), "2")
    groups = db.pending_notifications()
    assert sorted(len(v) for v in groups.values()) == [1, 2]


def test_state_roundtrip(db):
    assert db.get_state("k", "0") == "0"
    db.set_state("k", "3")
    db.set_state("k", "4")
    assert db.get_state("k") == "4"


def test_multi_job_story_sharing_one_link_is_not_deduped_by_url(db):
    db.record_story("1", 1, None)
    link = "https://jobs.bestbuy.com/ai"
    a = Job(company="Best Buy", role="AI Engineering Intern", application_url=link)
    b = Job(company="Best Buy", role="AI Product Intern", application_url=link)
    assert db.add_job(a, "1", use_url_key=False) is not None
    assert db.add_job(b, "1", use_url_key=False) is not None


def test_events_and_jobs_dedupe_separately(db):
    from scripts.job_extractor import Event
    db.record_story("1", 1, None)
    e = Event(organizer="Datadog", name="Software Engineer Intern", registration_url="https://x.com/e")
    db.add_job(DATADOG, "1")
    assert db.add_event(e, "1") is not None
    assert db.add_event(e, "1") is None
