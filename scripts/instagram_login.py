"""Manual Instagram login into the persistent browser profile.

  uv run python -m scripts.instagram_login                   # log in here (opens a browser window)
  uv run python -m scripts.instagram_login --export FILE     # log in here, then save the session to FILE
  uv run python -m scripts.instagram_login --import FILE     # load a saved session (headless; for servers/Docker)

The exported FILE is a live Instagram session. Treat it like a password: never commit it,
move it only over SSH/scp, and delete it after importing.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

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


def _verify(context, username: str) -> None:
    page = context.pages[0] if context.pages else context.new_page()
    page.goto(profile_url(username), wait_until="domcontentloaded")
    try:
        ensure_authenticated(context, page)
    except AuthExpiredError as e:
        raise SystemExit(str(e))
    print(f"Instagram session authenticated; {username} profile loaded.")


def interactive_login(export_path: Path | None) -> None:
    settings = load_settings()
    print(f"Browser profile: {settings.browser_profile_dir}")
    with open_browser(settings.browser_profile_dir, headless=False, channel=settings.browser_channel) as context:
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(BASE_URL)

        if not has_session_cookie(context):
            print("Log in to Instagram in the browser window (up to 10 minutes)...")
            deadline = time.monotonic() + LOGIN_TIMEOUT_SECONDS
            while not has_session_cookie(context):
                if time.monotonic() > deadline:
                    raise SystemExit("Timed out waiting for Instagram login.")
                page.wait_for_timeout(2000)

        _verify(context, settings.instagram_username)

        if export_path:
            # Only Instagram cookies — nothing from other sites in this profile.
            cookies = [c for c in context.cookies() if c["domain"].lstrip(".").endswith("instagram.com")]
            export_path.write_text(json.dumps({"cookies": cookies}))
            os.chmod(export_path, 0o600)
            print(f"Session exported to {export_path} (contains live credentials — delete after importing).")

        print("Session saved. You can close this window.")
        page.wait_for_timeout(3000)


def import_session(import_path: Path) -> None:
    settings = load_settings()
    cookies = json.loads(import_path.read_text()).get("cookies") or []
    if not any(c.get("name") == "sessionid" for c in cookies):
        raise SystemExit(f"{import_path} does not contain an Instagram session.")
    with open_browser(settings.browser_profile_dir, headless=True, channel=settings.browser_channel) as context:
        context.add_cookies(cookies)
        _verify(context, settings.instagram_username)
    print(f"Session imported into {settings.browser_profile_dir}. Delete {import_path} now.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Log in to Instagram for the monitor.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--export", type=Path, metavar="FILE", help="save the session to FILE after logging in")
    mode.add_argument("--import", dest="import_path", type=Path, metavar="FILE", help="load a session from FILE")
    args = parser.parse_args()

    if args.import_path:
        import_session(args.import_path)
    else:
        interactive_login(args.export)


if __name__ == "__main__":
    main()
