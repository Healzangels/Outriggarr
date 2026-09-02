"""Environment settings and DB-backed settings access.

Environment variables configure where the process lives (paths, DB, port).
Everything the GUI can edit lives in the `setting` table and is read through
`get_setting` / `set_setting`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from outriggarr.db.models import Setting


@dataclass(frozen=True)
class Settings:
    config_dir: Path
    staging_dir: Path
    database_url: str
    log_level: str

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        config_dir = Path(env.get("OUTRIGGARR_CONFIG_DIR", "/config"))
        staging_dir = Path(env.get("OUTRIGGARR_STAGING_DIR", "/staging"))
        database_url = env.get("OUTRIGGARR_DATABASE_URL", f"sqlite:///{config_dir / 'app.db'}")
        return cls(
            config_dir=config_dir,
            staging_dir=staging_dir,
            database_url=database_url,
            log_level=env.get("OUTRIGGARR_LOG_LEVEL", "INFO"),
        )


# Defaults for DB-backed settings. Keys are the only ones the app knows about.
DEFAULTS: dict[str, str] = {
    "scan_interval_minutes": "30",
    "concurrency": "1",
    "default_format": "bestvideo*+bestaudio/best",
    "merge_container": "mkv",
    "ytdlp_extra_opts": "{}",
    "cookies_path": "",
}


def get_setting(session: Session, key: str) -> str:
    if key not in DEFAULTS:
        raise KeyError(key)
    row = session.get(Setting, key)
    return DEFAULTS[key] if row is None else row.value


def set_setting(session: Session, key: str, value: str) -> None:
    if key not in DEFAULTS:
        raise KeyError(key)
    row = session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value


def all_settings(session: Session) -> dict[str, str]:
    return {key: get_setting(session, key) for key in DEFAULTS}
