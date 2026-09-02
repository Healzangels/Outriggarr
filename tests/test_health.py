from fastapi.testclient import TestClient

from outriggarr import __version__


def test_health_reports_ok_and_version(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__


def test_startup_creates_db_file(client: TestClient) -> None:
    settings = client.app.state.settings
    assert (settings.config_dir / "app.db").exists()
