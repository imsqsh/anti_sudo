"""One-time (or on expiry) manual Instagram login into the persistent browser profile.

Usage: uv run python -m scripts.instagram_login
"""

from __future__ import annotations

import time

from scripts.instagram import (
    AuthExpiredError,
    BASE_URL,
    ensure_authenticated,
    has_session_cookie,
    open_browser,
    profile_url,
)
from scripts.settings import load_settings

LOGIN_TIMEOUT_SECONDS = 600


def main() -> None:
    settings = load_settings()
    print(f"Browser profile: {settings.browser_profile_dir}")

    with open_browser(settings.browser_profile_dir, headless=False) as context:
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(BASE_URL)

        if not has_session_cookie(context):
            print("Log in to Instagram in the browser window (up to 10 minutes)...")
            deadline = time.monotonic() + LOGIN_TIMEOUT_SECONDS
            while not has_session_cookie(context):
                if time.monotonic() > deadline:
                    raise SystemExit("Timed out waiting for Instagram login.")
                page.wait_for_timeout(2000)

        page.goto(profile_url(settings.instagram_username))
        page.wait_for_load_state("domcontentloaded")
        try:
            ensure_authenticated(context, page)
        except AuthExpiredError as e:
            raise SystemExit(str(e))

        print(f"Instagram session authenticated; {settings.instagram_username} profile loaded.")
        print("Session saved. You can close this window.")
        page.wait_for_timeout(3000)


if __name__ == "__main__":
    main()
