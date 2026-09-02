from fastapi.testclient import TestClient

SONARR = {
    "kind": "sonarr",
    "name": "Sonarr",
    "url": "http://sonarr-host:1234",
    "api_key": "k1",
    "staging_path_remote": "/staging",
}


def test_cross_site_posts_are_refused(client: TestClient) -> None:
    client.post("/api/connections", json=SONARR)
    evil = {"Origin": "http://evil.example", "Sec-Fetch-Site": "cross-site"}
    r = client.post(
        "/settings/connections/1",
        data={
            "kind": "sonarr",
            "name": "X",
            "url": "http://attacker:9",
            "api_key": "",
            "staging_path_remote": "/s",
        },
        headers=evil,
    )
    assert r.status_code == 403 and "cross-site" in r.json()["detail"]
    assert client.get("/api/connections").json()[0]["url"] == "http://sonarr-host:1234"
    # Origin mismatch alone (older browsers) is enough
    r = client.post("/settings/notify/test", headers={"Origin": "http://elsewhere:1"})
    assert r.status_code == 403
    # JSON API is guarded the same way
    r = client.put(
        "/api/settings", json={"concurrency": "2"}, headers={"Referer": "http://elsewhere/x"}
    )
    assert r.status_code == 403


def test_same_origin_and_non_browser_requests_pass(client: TestClient) -> None:
    host = "testserver"
    r = client.post(
        "/api/connections",
        json=SONARR,
        headers={"Origin": f"http://{host}", "Sec-Fetch-Site": "same-origin"},
    )
    assert r.status_code == 201
    r = client.put("/api/settings", json={"concurrency": "2"})  # curl-style: no Origin at all
    assert r.status_code == 200
    r = client.post(
        "/settings/notify/test",
        headers={"Referer": f"http://{host}/settings", "Sec-Fetch-Site": "same-origin"},
    )
    assert r.status_code == 200  # 200 with the "no URLs" text, not 403
    assert client.get("/activity", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200, (
        "GETs are never blocked"
    )
