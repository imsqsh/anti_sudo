import json
from pathlib import Path

import pytest

from scripts.database import Database
from scripts.settings import Settings

CONFIG = {
    "instagram": {"username": "zero2sudo"},
    "llm": {"model": "test/model"},
    "filtering": {
        "employment_types": ["internship", "new_grad"],
        "categories": [
            "software_engineering", "machine_learning", "artificial_intelligence",
            "data_engineering", "systems", "infrastructure", "cybersecurity", "research",
        ],
        "exclude_countries": ["Canada"],
        "min_hourly_usd": 30,
        "education": {"degree_levels": ["bachelors", "masters"], "majors": ["Computer Science"]},
        "send_events": True,
    },
}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        instagram_username="zero2sudo",
        llm_model="test/model",
        whatsapp_target="+15555550123",
        dry_run=False,
        data_dir=tmp_path,
        config=CONFIG,
    )


@pytest.fixture
def db(settings):
    database = Database(settings.db_path)
    yield database
    database.close()


def story_item(pk: str, taken_at: int = 1_700_000_000, link: str | None = None, media_type: int = 2) -> dict:
    item = {
        "pk": pk,
        "id": f"{pk}_123",
        "taken_at": taken_at,
        "media_type": media_type,
        "image_versions2": {"candidates": [
            {"url": f"https://cdn.example/{pk}_1080.jpg", "width": 1080, "height": 1920},
            {"url": f"https://cdn.example/{pk}_640.jpg", "width": 640, "height": 1138},
            {"url": f"https://cdn.example/{pk}_320.jpg", "width": 320, "height": 569},
        ]},
        "story_link_stickers": None,
    }
    if link:
        item["story_link_stickers"] = [{"story_link": {"url": link}}]
    return item


def stories_page(items: list[dict], username: str = "zero2sudo", seen: int = 0) -> str:
    """Minimal HTML shaped like Instagram's /stories/<user>/ page."""
    payload = {"require": [["x", "y", None, [{"__bbox": {"result": {"data": {
        "xdt_api__v1__feed__reels_media": {"reels_media": [
            {"id": "1", "user": {"username": username}, "seen": seen, "items": items}
        ]}
    }}}}]]]}
    return (
        "<html><body>"
        '<script type="application/json">{"unrelated": true}</script>'
        f'<script type="application/json" data-sjs>{json.dumps(payload)}</script>'
        "</body></html>"
    )
