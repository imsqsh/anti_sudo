"""All Instagram-specific browser logic lives here.

The rest of the app should not know about Instagram URLs, cookies, or selectors.
Never log cookie values.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlunparse

from playwright.sync_api import BrowserContext, Page, sync_playwright

BASE_URL = "https://www.instagram.com"
LOGIN_PATH = "/accounts/login"
SESSION_COOKIE = "sessionid"


class AuthExpiredError(RuntimeError):
    """The persistent session is no longer logged in; a human must reauthenticate."""


@contextmanager
def open_browser(profile_dir: Path, headless: bool, channel: str | None = "chrome") -> Iterator[BrowserContext]:
    """Persistent browser profile so the Instagram login survives between runs.

    channel="chrome" uses the installed Google Chrome; None uses Playwright's bundled Chromium
    (what the Docker image uses).
    """
    profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            channel=channel,
            headless=headless,
            viewport={"width": 1280, "height": 900},
        )
        try:
            yield context
        finally:
            context.close()


def has_session_cookie(context: BrowserContext) -> bool:
    """True if an Instagram session cookie exists. Only checks presence, never reads the value out."""
    return any(c["name"] == SESSION_COOKIE for c in context.cookies(BASE_URL))


def is_login_page(page: Page) -> bool:
    return LOGIN_PATH in page.url


def ensure_authenticated(context: BrowserContext, page: Page) -> None:
    """Raise AuthExpiredError if the profile is not logged in."""
    if not has_session_cookie(context) or is_login_page(page):
        raise AuthExpiredError(
            "Instagram authentication has expired. "
            "Please reauthenticate the persistent browser session."
        )


def profile_url(username: str) -> str:
    return f"{BASE_URL}/{username}/"


@dataclass(frozen=True)
class Story:
    story_key: str          # Instagram media pk — stable across page loads
    taken_at: int           # unix seconds
    media_type: int         # 1 = image, 2 = video
    image_url: str          # cover frame (for videos: the first frame)
    link_url: str | None    # unwrapped link sticker destination
    viewed: bool = False    # the logged-in account has already watched this Story


class PageChangedError(RuntimeError):
    """Instagram's page structure no longer matches what we parse."""


_JSON_SCRIPT_RE = re.compile(r'<script type="application/json"[^>]*>(.*?)</script>', re.S)


def _find_reels(obj) -> list[dict]:
    """Recursively find reel objects: dicts whose `items` are story media (have taken_at)."""
    found = []
    stack = [obj]
    while stack:
        o = stack.pop()
        if isinstance(o, dict):
            items = o.get("items")
            if isinstance(items, list) and items and isinstance(items[0], dict) and "taken_at" in items[0]:
                found.append(o)
                continue
            stack.extend(o.values())
        elif isinstance(o, list):
            stack.extend(o)
    return found


def _best_image_url(item: dict) -> str | None:
    candidates = (item.get("image_versions2") or {}).get("candidates") or []
    if not candidates:
        return None
    # Smallest candidate that's still legible (>= 640px wide), else the largest.
    candidates = sorted(candidates, key=lambda c: c.get("width") or 0)
    legible = [c for c in candidates if (c.get("width") or 0) >= 640]
    return (legible[0] if legible else candidates[-1]).get("url")


def parse_stories(html: str, username: str) -> list[Story]:
    """Extract the account's active Stories from the /stories/<user>/ page HTML."""
    reels = []
    for blob in _JSON_SCRIPT_RE.findall(html):
        if '"taken_at"' not in blob:
            continue
        try:
            reels.extend(_find_reels(json.loads(blob)))
        except json.JSONDecodeError:
            continue

    stories: dict[str, Story] = {}
    for reel in reels:
        owner = ((reel.get("user") or {}).get("username") or "").lower()
        if owner and owner != username.lower():
            continue
        seen = reel.get("seen") or 0  # unix time of the last Story this account viewed
        for item in reel["items"]:
            pk = str(item.get("pk") or item.get("id", "")).split("_")[0]
            image_url = _best_image_url(item)
            if not pk or not image_url:
                continue
            links = [
                unwrap_link((s.get("story_link") or {}).get("url"))
                for s in item.get("story_link_stickers") or []
            ]
            stories[pk] = Story(
                story_key=pk,
                taken_at=int(item["taken_at"]),
                media_type=int(item.get("media_type") or 0),
                image_url=image_url,
                link_url=next((l for l in links if l), None),
                viewed=bool(seen) and int(item["taken_at"]) <= int(seen),
            )
    return sorted(stories.values(), key=lambda s: s.taken_at)


def fetch_stories(context: BrowserContext, username: str) -> list[Story]:
    """Load the Stories page (without opening/viewing a Story) and parse it.

    Returns [] when the account has no active Stories.
    Raises AuthExpiredError / PageChangedError.
    """
    page = context.pages[0] if context.pages else context.new_page()
    page.goto(f"{BASE_URL}/stories/{username}/", wait_until="domcontentloaded")
    ensure_authenticated(context, page)

    if f"/stories/{username}" not in page.url:
        # Instagram redirects to the profile when there are no active Stories.
        return []

    stories = parse_stories(page.content(), username)
    if not stories and page.get_by_role("button", name="View story").count() > 0:
        raise PageChangedError("Stories page loaded but no Story data could be parsed")
    return stories


def download_image(context: BrowserContext, url: str, dest: Path) -> Path:
    """Fetch a Story image through the browser's own session."""
    response = context.request.get(url)
    if not response.ok:
        raise RuntimeError(f"Image download failed: HTTP {response.status}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(response.body())
    return dest


TRACKING_PARAMS = {"fbclid", "igshid", "igsh"}


def unwrap_link(url: str | None) -> str | None:
    """Turn an l.instagram.com redirect into the real destination, minus tracking params."""
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.netloc.endswith("l.instagram.com"):
        target = parse_qs(parsed.query).get("u")
        if not target:
            return None
        parsed = urlparse(target[0])
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k.lower() not in TRACKING_PARAMS and not k.lower().startswith("utm_")]
    return urlunparse(parsed._replace(query=urlencode(query)))
