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


SONARR_BODY = {
    "kind": "sonarr",
    "name": "Sonarr",
    "url": "http://sonarr-host:1234",
    "api_key": "k1",
    "staging_path_remote": "/data/outriggarr",
}


def test_origin_null_is_cross_site(client) -> None:
    # a sandboxed iframe / data: page / no-referrer form: never one of our pages, and a
    # non-browser client sends no Origin at all
    from outriggarr.db.models import Connection

    r = client.post("/api/connections", json=SONARR_BODY)
    conn_id = r.json()["id"]
    attack = client.post(
        f"/settings/connections/{conn_id}",
        data={
            "kind": "sonarr",
            "name": "Sonarr",
            "url": "http://attacker:9",
            "api_key": "",
            "staging_path_remote": "/data/outriggarr",
        },
        headers={"Origin": "null"},
    )
    assert attack.status_code == 403, attack.text
    with client.app.state.session_factory() as s:
        assert s.get(Connection, conn_id).url == SONARR_BODY["url"], "not re-pointed"
    same = client.post(
        f"/settings/connections/{conn_id}",
        data={
            "kind": "sonarr",
            "name": "Sonarr",
            "url": SONARR_BODY["url"],
            "api_key": "",
            "staging_path_remote": "/data/outriggarr",
        },
        headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
        follow_redirects=False,
    )
    assert same.status_code in (200, 303), "an HTMX-style same-origin post passes"


def test_origin_null_with_a_same_origin_verdict_passes() -> None:
    # a same-origin form under Referrer-Policy: no-referrer sends Origin: null; the
    # browser's Sec-Fetch-Site verdict is the truth
    from starlette.requests import Request

    from outriggarr.web.middleware import cross_site

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/x",
        "headers": [
            (b"host", b"app:8080"),
            (b"origin", b"null"),
            (b"sec-fetch-site", b"same-origin"),
        ],
        "query_string": b"",
    }
    assert cross_site(Request(scope)) is None
    scope["headers"][2] = (b"sec-fetch-site", b"cross-site")
    assert cross_site(Request(scope)) == "Sec-Fetch-Site: cross-site"
