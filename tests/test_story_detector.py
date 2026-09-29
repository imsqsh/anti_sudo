from scripts.instagram import parse_stories, unwrap_link
from tests.conftest import stories_page, story_item

WRAPPED = (
    "https://l.instagram.com/?u=https%3A%2F%2Fwww.citadel.com%2Fcareers%2Fdetails%2Fintern%2F"
    "%3Ffbclid%3DABC123%26gh_jid%3D42&e=AUDxyz"
)


def test_parse_stories_extracts_keys_links_and_images():
    html = stories_page([
        story_item("200", taken_at=2, link=WRAPPED),
        story_item("100", taken_at=1),
    ])
    stories = parse_stories(html, "zero2sudo")

    assert [s.story_key for s in stories] == ["100", "200"]  # sorted by time
    assert stories[0].link_url is None
    assert stories[1].link_url == "https://www.citadel.com/careers/details/intern/?gh_jid=42"
    assert stories[1].image_url == "https://cdn.example/200_640.jpg"  # smallest legible


def test_parse_stories_marks_already_viewed():
    html = stories_page([story_item("1", taken_at=100), story_item("2", taken_at=200)], seen=150)
    assert [(s.story_key, s.viewed) for s in parse_stories(html, "zero2sudo")] == [("1", True), ("2", False)]


def test_parse_stories_nothing_viewed_when_seen_missing():
    html = stories_page([story_item("1", taken_at=100)], seen=0)
    assert parse_stories(html, "zero2sudo")[0].viewed is False


def test_parse_stories_ignores_other_accounts():
    html = stories_page([story_item("1")], username="someone_else")
    assert parse_stories(html, "zero2sudo") == []


def test_parse_stories_handles_no_story_data():
    assert parse_stories("<html><body>nothing</body></html>", "zero2sudo") == []


def test_story_key_is_stable_across_page_loads():
    a = parse_stories(stories_page([story_item("555", link=WRAPPED)]), "zero2sudo")
    b = parse_stories(stories_page([story_item("555", link=WRAPPED)]), "zero2sudo")
    assert a == b


def test_unwrap_link_strips_redirect_and_tracking():
    assert unwrap_link(WRAPPED) == "https://www.citadel.com/careers/details/intern/?gh_jid=42"
    assert unwrap_link("https://jobs.example.com/x?fbclid=1") == "https://jobs.example.com/x"
    assert unwrap_link("https://jobs.example.com/x?utm_source=zero2sudo&id=7") == "https://jobs.example.com/x?id=7"
    assert unwrap_link(None) is None
    assert unwrap_link("https://l.instagram.com/?e=only") is None


def test_new_story_detection(db):
    assert db.record_story("1", 1, None) is True
    assert db.needs_processing("1")


def test_duplicate_story_detection(db):
    db.record_story("1", 1, None)
    db.mark_classified("1", is_job=False, relevance="irrelevant", confidence=0.9)
    assert db.record_story("1", 1, None) is False
    assert not db.needs_processing("1")
