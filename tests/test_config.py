import json

import pytest

from lingtai_api_pool.config import ConfigError, load_config, parse_config
from tests.conftest import make_config_data


def test_load_valid_config(config_file):
    config = load_config(config_file)
    assert [u.id for u in config.upstreams] == ["primary", "secondary"]
    assert config.pool.max_attempts == 2
    assert config.pool.retry_statuses == frozenset({429, 503})
    assert config.upstreams[1].weight == 1.0  # default
    assert config.upstreams[1].timeout_seconds == 60.0  # default


def test_summary_is_secret_safe(monkeypatch):
    monkeypatch.setenv("PRIMARY_AUTH", "Bearer super-secret-token")
    config = parse_config(make_config_data())
    summary = config.summary()
    blob = json.dumps(summary)
    # secret value must never appear; only the env var *name* may
    assert "super-secret-token" not in blob
    assert "PRIMARY_AUTH" in blob
    primary = next(u for u in summary["upstreams"] if u["id"] == "primary")
    assert primary["header_env"] == {"authorization": "PRIMARY_AUTH"}
    assert primary["static_header_keys"] == ["x-client"]


def test_resolve_headers_reads_env(monkeypatch):
    monkeypatch.setenv("PRIMARY_AUTH", "Bearer tok")
    config = parse_config(make_config_data())
    primary = config.upstreams[0]
    headers = primary.resolve_headers()
    assert headers["authorization"] == "Bearer tok"
    assert headers["x-client"] == "lingtai"


def test_missing_env_reported_not_resolved(monkeypatch):
    monkeypatch.delenv("PRIMARY_AUTH", raising=False)
    config = parse_config(make_config_data())
    primary = config.upstreams[0]
    assert "PRIMARY_AUTH" in primary.missing_env()
    assert "authorization" not in primary.resolve_headers()


def test_requires_at_least_one_upstream():
    with pytest.raises(ConfigError, match="at least one"):
        parse_config({"upstream": []})


def test_rejects_duplicate_ids():
    data = {
        "upstream": [
            {"id": "dup", "base_url": "https://a.test"},
            {"id": "dup", "base_url": "https://b.test"},
        ]
    }
    with pytest.raises(ConfigError, match="duplicate"):
        parse_config(data)


def test_rejects_bad_base_url():
    data = {"upstream": [{"id": "x", "base_url": "ftp://nope"}]}
    with pytest.raises(ConfigError, match="http"):
        parse_config(data)


def test_rejects_nonpositive_weight():
    data = {"upstream": [{"id": "x", "base_url": "https://a.test", "weight": 0}]}
    with pytest.raises(ConfigError, match="weight"):
        parse_config(data)


def test_missing_file():
    with pytest.raises(ConfigError, match="not found"):
        load_config("/nonexistent/path/config.toml")
