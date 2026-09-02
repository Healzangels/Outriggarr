from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from outriggarr.db.session import make_engine, make_session_factory, run_migrations
from outriggarr.main import create_app
from outriggarr.settings import Settings
from tests.fakes import FakeArrFactory, FakeNotifier, FakeVideoSource


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _clear_library_cache():
    from outriggarr.api.library import library_cache

    library_cache.clear()
    yield
    library_cache.clear()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings.from_env(
        {
            "OUTRIGGARR_CONFIG_DIR": str(tmp_path / "config"),
            "OUTRIGGARR_STAGING_DIR": str(tmp_path / "staging"),
            "OUTRIGGARR_LOG_LEVEL": "WARNING",
        }
    )


@pytest.fixture
def session_factory(settings: Settings):
    settings.config_dir.mkdir(parents=True)
    run_migrations(settings.database_url)
    engine = make_engine(settings.database_url)
    yield make_session_factory(engine)
    engine.dispose()


@pytest.fixture
def arr() -> FakeArrFactory:
    return FakeArrFactory()


@pytest.fixture
def source() -> FakeVideoSource:
    return FakeVideoSource()


@pytest.fixture
def notifier() -> FakeNotifier:
    return FakeNotifier()


@pytest.fixture
def client(
    settings: Settings, arr: FakeArrFactory, source: FakeVideoSource, notifier: FakeNotifier
) -> Iterator[TestClient]:
    with TestClient(
        create_app(settings, start_worker=False, arr_factory=arr, source=source, notifier=notifier)
    ) as c:
        yield c
