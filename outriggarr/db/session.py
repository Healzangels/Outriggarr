"""Engine/session construction and migration runner."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

log = logging.getLogger(__name__)

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

SessionFactory = sessionmaker[Session]


def make_engine(database_url: str) -> Engine:
    # timeout: how long a writer waits for the single SQLite write lock instead of
    # failing with "database is locked" (the progress hook writes every 2 s).
    engine = create_engine(database_url, connect_args={"check_same_thread": False, "timeout": 30})

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record) -> None:  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        mode = cur.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(mode).lower() != "wal":  # a filesystem that refuses WAL (some FUSE/NFS)
            log.warning("SQLite journal mode is %s, not WAL: writes will block readers", mode)
        cur.close()

    return engine


def make_session_factory(engine: Engine) -> SessionFactory:
    return sessionmaker(bind=engine, expire_on_commit=False)


def alembic_config(database_url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", database_url)
    cfg.attributes["configure_logging"] = False
    return cfg


def backup_before_upgrade(database_url: str) -> Path | None:
    """A copy of a SQLite database that is about to change schema, next to it as
    app.db.bak-<revision>: an image rolled back to older code has something to return
    to. None when the schema is current or the database does not exist yet."""
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    if not database_url.startswith("sqlite:///"):
        return None
    path = Path(database_url.removeprefix("sqlite:///"))
    if not path.is_file():
        return None
    cfg = alembic_config(database_url)
    head = ScriptDirectory.from_config(cfg).get_current_head()
    engine = create_engine(database_url)
    try:
        with engine.connect() as conn:
            current = MigrationContext.configure(conn).get_current_revision()
    finally:
        engine.dispose()
    if current == head:
        return None
    target = path.with_name(f"{path.name}.bak-{current or 'empty'}")
    with sqlite3.connect(path) as src, sqlite3.connect(target) as dst:
        src.backup(dst)  # the online backup API: consistent even mid-WAL
    log.warning("schema %s -> %s: backed up the database to %s", current, head, target)
    return target


def run_migrations(database_url: str) -> None:
    log.info("running migrations")
    backup_before_upgrade(database_url)
    command.upgrade(alembic_config(database_url), "head")
