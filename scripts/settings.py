"""Loads config.yaml and .env into a single Settings object."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader. Real environment variables take precedence."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    instagram_username: str
    llm_model: str
    whatsapp_target: str | None
    dry_run: bool
    data_dir: Path
    config: dict

    @property
    def db_path(self) -> Path:
        return self.data_dir / "database.sqlite"

    @property
    def browser_profile_dir(self) -> Path:
        return self.data_dir / "browser-profile"

    @property
    def screenshots_dir(self) -> Path:
        return self.data_dir / "screenshots"


def load_settings(config_path: Path | None = None) -> Settings:
    _load_dotenv(REPO_ROOT / ".env")
    config = yaml.safe_load((config_path or REPO_ROOT / "config.yaml").read_text())

    data_dir = Path(os.environ.get("DATA_DIR", "~/openclaw-instagram-jobs")).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)

    return Settings(
        instagram_username=config["instagram"]["username"],
        llm_model=config["llm"]["model"],
        whatsapp_target=os.environ.get("WHATSAPP_TARGET") or None,
        dry_run=os.environ.get("DRY_RUN", "false").lower() in ("1", "true", "yes"),
        data_dir=data_dir,
        config=config,
    )
