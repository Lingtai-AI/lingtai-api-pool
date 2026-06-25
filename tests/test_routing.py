import json

from starlette.datastructures import Headers

from lingtai_api_pool.routing import (
    extract_sticky_key,
    select_least_inflight,
    select_sticky,
)


# --- sticky key extraction -------------------------------------------------


def _headers(**kw):
    return Headers(raw=[(k.encode(), v.encode()) for k, v in kw.items()])


def test_header_session_id_takes_priority():
    headers = _headers(session_id="S", thread_id="T", **{"x-codex-window-id": "W"})
    assert extract_sticky_key(headers, None) == "S"


def test_thread_id_when_no_session():
    headers = _headers(thread_id="T", **{"x-codex-window-id": "W"})
    assert extract_sticky_key(headers, None) == "T"


def test_codex_window_id_when_no_session_or_thread():
    headers = _headers(**{"x-codex-window-id": "W"})
    assert extract_sticky_key(headers, None) == "W"


def test_body_prompt_cache_key():
    body = json.dumps({"prompt_cache_key": "PCK", "session_id": "SB"}).encode()
    assert extract_sticky_key(_headers(), body) == "PCK"


def test_body_session_id_fallback():
    body = json.dumps({"session_id": "SB"}).encode()
    assert extract_sticky_key(_headers(), body) == "SB"


def test_header_beats_body():
    body = json.dumps({"prompt_cache_key": "PCK"}).encode()
    assert extract_sticky_key(_headers(session_id="S"), body) == "S"


def test_no_sticky_key():
    assert extract_sticky_key(_headers(), b'{"messages": []}') is None
    assert extract_sticky_key(_headers(), None) is None
    assert extract_sticky_key(_headers(), b"not json") is None


# --- rendezvous selection --------------------------------------------------


def test_same_key_same_upstream(app_config):
    ups = list(app_config.upstreams)
    first = select_sticky("session-abc", ups)
    for _ in range(20):
        assert select_sticky("session-abc", ups).id == first.id


def test_deterministic_independent_of_order(app_config):
    ups = list(app_config.upstreams)
    a = select_sticky("k", ups)
    b = select_sticky("k", list(reversed(ups)))
    assert a.id == b.id


def test_different_keys_can_route_differently(app_config):
    ups = list(app_config.upstreams)
    chosen = {select_sticky(f"key-{i}", ups).id for i in range(100)}
    # over many keys, both upstreams should get traffic
    assert chosen == {"primary", "secondary"}


def test_sticky_redistributes_when_upstream_removed(app_config):
    ups = list(app_config.upstreams)
    # key that lands on whichever; removing it must still yield a valid choice
    only_secondary = [u for u in ups if u.id == "secondary"]
    assert select_sticky("anything", only_secondary).id == "secondary"


def test_select_sticky_empty():
    assert select_sticky("k", []) is None


# --- least in-flight -------------------------------------------------------


def test_least_inflight_prefers_idle(app_config):
    ups = list(app_config.upstreams)
    selected = select_least_inflight(ups, {"primary": 5, "secondary": 0})
    assert selected.id == "secondary"


def test_least_inflight_weight_normalizes_load(app_config):
    ups = list(app_config.upstreams)
    # primary weight 2, secondary weight 1; equal raw load -> primary less loaded
    selected = select_least_inflight(ups, {"primary": 2, "secondary": 2})
    assert selected.id == "primary"
