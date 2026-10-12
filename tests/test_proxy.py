"""Offline ASGI wire smoke, not live upstream or incremental-stream acceptance."""

import json

import httpx
from starlette.testclient import TestClient

from lingtai_api_pool.proxy import create_app


def build_client(handler, config):
    transport = httpx.MockTransport(handler)
    httpx_client = httpx.AsyncClient(transport=transport)
    app = create_app(config, client=httpx_client)
    return TestClient(app), app


def test_forwards_and_preserves_response(monkeypatch, app_config):
    monkeypatch.setenv("PRIMARY_AUTH", "Bearer p-secret")
    monkeypatch.setenv("SECONDARY_AUTH", "Bearer s-secret")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["body"] = request.content
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(
            201,
            json={"ok": True},
            headers={"content-type": "application/json", "x-upstream": "yes"},
        )

    client, _ = build_client(handler, app_config)
    resp = client.post(
        "/v1/responses",
        headers={"session_id": "abc"},
        content=json.dumps({"input": "hi"}),
    )

    assert resp.status_code == 201
    assert resp.json() == {"ok": True}
    assert resp.headers["content-type"] == "application/json"
    assert resp.headers["x-upstream"] == "yes"
    assert seen["method"] == "POST"
    assert seen["url"].rstrip("?").endswith("/v1/responses")
    assert seen["body"] == json.dumps({"input": "hi"}).encode()
    # per-upstream auth header was applied from env (selected upstream's secret)
    assert seen["auth"] in ("Bearer p-secret", "Bearer s-secret")


def test_all_upstreams_failing_returns_502(app_config):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client, _ = build_client(handler, app_config)
    resp = client.post("/v1/responses", headers={"session_id": "k1"}, content="{}")
    assert resp.status_code == 502


def test_does_not_retry_indefinitely(app_config):
    """max_attempts caps the number of upstream calls."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503)

    config = app_config
    client, _ = build_client(handler, config=config)
    client.post("/v1/responses", headers={"session_id": "k1"}, content="{}")
    # only 2 upstreams exist; cannot exceed that even with max_attempts=3
    assert calls["n"] == 2


def test_sse_streaming_passthrough(app_config):
    def handler(request: httpx.Request) -> httpx.Response:
        body = b"data: one\n\ndata: two\n\n"
        return httpx.Response(
            200, content=body, headers={"content-type": "text/event-stream"}
        )

    client, _ = build_client(handler, app_config)
    resp = client.post("/v1/responses", headers={"session_id": "x"}, content="{}")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert b"data: one" in resp.content
    assert b"data: two" in resp.content
