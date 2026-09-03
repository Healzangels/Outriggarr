import pytest
from fastapi.testclient import TestClient

from outriggarr import __version__


def test_health_reports_ok_and_version(client: TestClient, monkeypatch) -> None:
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")  # ffmpeg + deno present
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__


def test_startup_creates_db_file(client: TestClient) -> None:
    settings = client.app.state.settings
    assert (settings.config_dir / "app.db").exists()


def test_health_degrades_when_ffmpeg_or_staging_or_worker_is_gone(
    client: TestClient, monkeypatch
) -> None:
    import asyncio
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None)
    r = client.get("/health")
    assert r.status_code == 503
    assert r.json()["status"] == "degraded" and "ffmpeg" in r.json()["problems"]

    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    settings = client.app.state.settings
    shutil.rmtree(settings.staging_dir, ignore_errors=True)
    r = client.get("/health")
    assert r.status_code == 503 and r.json()["problems"] == ["staging_writable"]
    settings.staging_dir.mkdir(parents=True, exist_ok=True)

    async def boom():
        raise RuntimeError("worker crashed")

    loop = asyncio.new_event_loop()
    dead = loop.create_task(boom())
    with __import__("contextlib").suppress(RuntimeError):
        loop.run_until_complete(dead)
    client.app.state.background_tasks = {"worker": dead, "scheduler": None}
    r = client.get("/health")
    assert r.status_code == 503 and r.json()["problems"] == ["worker"]
    assert r.json()["worker_alive"] is False and r.json()["scheduler_alive"] is None
    client.app.state.background_tasks = {}
    loop.close()


def test_health_reports_po_token_provider(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["po_token_provider"] is False, "the test image has no bgutil checkout"
    assert "po_token_provider" not in body.get("problems", []), "optional: never degrades"
    assert "PO tokens: off" in client.get("/activity").text
    assert (
        body["youtube_session"] == "none" and "YouTube: no cookies" in client.get("/activity").text
    )


def test_staging_probe_logs_each_flip_with_the_reason(tmp_path, caplog, monkeypatch) -> None:
    import logging
    import os

    from outriggarr.api import health

    if os.geteuid() == 0:
        pytest.skip("root can write to a read-only directory")
    monkeypatch.setattr(health, "_last_staging_state", None)
    staging = tmp_path / "staging"
    staging.mkdir()
    with caplog.at_level(logging.INFO, logger="outriggarr.api.health"):
        assert health.staging_writable(staging) is True
        staging.chmod(0o555)
        try:
            assert health.staging_writable(staging) is False
            assert health.staging_writable(staging) is False  # same answer: no second line
        finally:
            staging.chmod(0o755)
        staging.rmdir()
        assert health.staging_writable(staging) is False
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "NOT writable" in warnings[0], warnings
    assert "mode=dr-xr-xr-x" in warnings[0] and f"uid {os.geteuid()}" in warnings[0]
    assert "is writable" in caplog.records[0].getMessage()
    # the directory vanishing is a different reason, but the answer did not change: silent
    assert sum("NOT writable" in r.getMessage() for r in caplog.records) == 1


def test_health_and_footer_show_a_rate_limit_pause(client: TestClient) -> None:
    before = client.get("/health").json()
    assert before["youtube_cooloff"] is None
    cooloff = client.app.state.runner_deps.cooloff
    cooloff.hit("ERROR: rate-limited by YouTube for up to an hour.")
    body = client.get("/health").json()
    assert body["status"] == before["status"], "a pause is not a degradation: it lifts by itself"
    assert 890 <= body["youtube_cooloff"]["remaining_seconds"] <= 900
    assert body["youtube_cooloff"]["message"].startswith("ERROR: rate-limited")
    page = client.get("/activity").text
    assert "rate-limited: paused 15 min" in page
    cooloff.clear()
    assert client.get("/health").json()["youtube_cooloff"] is None
    assert "rate-limited: paused" not in client.get("/activity").text


def test_health_is_degraded_when_another_instance_holds_the_database(client) -> None:
    client.app.state.worker_note = "Another Outriggarr instance holds this database"
    try:
        r = client.get("/health")
        assert r.status_code == 503 and "instance_lock" in r.json()["problems"]
    finally:
        del client.app.state.worker_note
