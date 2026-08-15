import json

import httpx
import pytest
from starlette.testclient import TestClient

from lingtai_api_pool.config import parse_config
from lingtai_api_pool.proxy import create_app
from tests.conftest import make_config_data


def build_client(handler, config=None):
    config = config or parse_config(make_config_data())
    transport = httpx.MockTransport(handler)
    httpx_client = httpx.AsyncClient(transport=transport)
    app = create_app(config, client=httpx_client)
    return TestClient(app), app


def test_forwards_and_preserves_response(monkeypatch):
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

    client, _ = build_client(handler)
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


def test_query_string_preserved():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"q": request.url.query.decode()})

    client, _ = build_client(handler)
    resp = client.get("/v1/models?limit=5&order=desc", headers={"session_id": "x"})
    assert resp.status_code == 200
    assert "limit=5" in resp.json()["q"]


def test_upstream_base_path_prefix_preserved():
    data = make_config_data()
    data["upstream"] = [
        {
            "id": "prefixed",
            "base_url": "https://api.primary.test/base",
        }
    ]
    config = parse_config(data)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"path": request.url.path})

    client, _ = build_client(handler, config=config)
    resp = client.post("/v1/responses", content="{}")

    assert resp.status_code == 200
    assert resp.json()["path"] == "/base/v1/responses"


def test_rejects_parent_path_escape():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid path must not reach upstream")

    client, _ = build_client(handler)
    resp = client.post("/%2e%2e/v2/responses", content="{}")

    assert resp.status_code == 400
    assert resp.json() == {"error": "invalid path"}


def test_hop_by_hop_headers_stripped():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"keys": sorted(k.lower() for k in request.headers.keys())},
        )

    client, _ = build_client(handler)
    resp = client.post(
        "/v1/responses",
        headers={"session_id": "x", "te": "trailers", "upgrade": "h2c"},
        content="{}",
    )
    keys = resp.json()["keys"]
    # Client-supplied hop-by-hop headers must not reach the upstream.
    # (connection/host/content-length are re-added by httpx's own transport and
    # are not forwarded from the client, so we assert on te/upgrade instead.)
    assert "te" not in keys
    assert "upgrade" not in keys


def test_sticky_key_routes_consistently():
    """Same session_id always reaches the same upstream base_url."""
    hits = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request.url.host)
        return httpx.Response(200, json={})

    client, _ = build_client(handler)
    for _ in range(5):
        client.post("/v1/responses", headers={"session_id": "stable"}, content="{}")
    assert len(set(hits)) == 1


def test_failover_on_503():
    """First upstream (by routing) returns 503; request still succeeds via the other."""
    statuses = {}

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        # secondary always healthy, primary always 503
        if host == "api.primary.test":
            return httpx.Response(503, text="overloaded")
        return httpx.Response(200, json={"served_by": host})

    client, app = build_client(handler)
    # "k0" deterministically routes to primary first (see rendezvous hashing);
    # primary 503s, so the request must fail over to secondary.
    resp = client.post("/v1/responses", headers={"session_id": "k0"}, content="{}")
    assert resp.status_code == 200
    assert resp.json()["served_by"] == "api.secondary.test"
    # primary should be marked on cooldown after failing
    assert app.state.proxy.pool.is_on_cooldown("primary")


def test_failover_on_connection_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.primary.test":
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json={"served_by": request.url.host})

    client, app = build_client(handler)
    resp = client.post("/v1/responses", headers={"session_id": "k0"}, content="{}")
    assert resp.status_code == 200
    assert resp.json()["served_by"] == "api.secondary.test"


def test_all_upstreams_failing_returns_502():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client, _ = build_client(handler)
    resp = client.post("/v1/responses", headers={"session_id": "k1"}, content="{}")
    assert resp.status_code == 502


def test_does_not_retry_indefinitely():
    """max_attempts caps the number of upstream calls."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503)

    config = parse_config(make_config_data(max_attempts=3))
    client, _ = build_client(handler, config=config)
    client.post("/v1/responses", headers={"session_id": "k1"}, content="{}")
    # only 2 upstreams exist; cannot exceed that even with max_attempts=3
    assert calls["n"] == 2


def test_sse_streaming_passthrough():
    def handler(request: httpx.Request) -> httpx.Response:
        body = b"data: one\n\ndata: two\n\n"
        return httpx.Response(
            200, content=body, headers={"content-type": "text/event-stream"}
        )

    client, _ = build_client(handler)
    resp = client.post("/v1/responses", headers={"session_id": "x"}, content="{}")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert b"data: one" in resp.content
    assert b"data: two" in resp.content


def test_stream_true_in_body_passthrough():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"chunked-bytes", headers={"content-type": "application/json"})

    client, _ = build_client(handler)
    resp = client.post(
        "/v1/responses",
        headers={"session_id": "x"},
        content=json.dumps({"stream": True}),
    )
    assert resp.status_code == 200
    assert resp.content == b"chunked-bytes"
