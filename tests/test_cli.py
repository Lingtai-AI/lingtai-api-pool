import json

import pytest

from lingtai_api_pool.cli import main


def test_check_prints_secret_safe_summary(config_file, monkeypatch, capsys):
    monkeypatch.setenv("PRIMARY_AUTH", "Bearer top-secret")
    rc = main(["check", "--config", str(config_file)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "top-secret" not in out
    assert "PRIMARY_AUTH" in out
    summary = json.loads(out.split("\n\n")[0])
    assert {u["id"] for u in summary["upstreams"]} == {"primary", "secondary"}


def test_check_warns_on_missing_env(config_file, monkeypatch, capsys):
    monkeypatch.delenv("PRIMARY_AUTH", raising=False)
    rc = main(["check", "--config", str(config_file)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "not set" in out
    assert "PRIMARY_AUTH" in out


def test_check_bad_config_returns_2(tmp_path, capsys):
    bad = tmp_path / "bad.toml"
    bad.write_text("this is = not valid toml ][")
    rc = main(["check", "--config", str(bad)])
    err = capsys.readouterr().err
    assert rc == 2
    assert "config error" in err


def test_route_prints_selected_upstream(config_file, capsys):
    rc = main(["route", "--config", str(config_file), "--session-id", "sess-1"])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["session_id"] == "sess-1"
    assert payload["upstream_id"] in {"primary", "secondary"}
    assert payload["strategy"] == "weighted-rendezvous"


def test_route_is_deterministic(config_file, capsys):
    main(["route", "--config", str(config_file), "--session-id", "sess-1"])
    first = json.loads(capsys.readouterr().out)["upstream_id"]
    main(["route", "--config", str(config_file), "--session-id", "sess-1"])
    second = json.loads(capsys.readouterr().out)["upstream_id"]
    assert first == second
