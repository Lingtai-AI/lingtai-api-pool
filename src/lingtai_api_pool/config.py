"""Configuration loading, validation, and secret-safe summarization.

Config is TOML, read with the stdlib :mod:`tomllib`. Nothing here ever resolves
or prints secret values: ``header_env`` maps an outbound header name to an
*environment variable name*, and summaries expose only header keys and env var
names, never the values behind them.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


class ConfigError(ValueError):
    """Raised when a config file is missing, malformed, or invalid."""


@dataclass(frozen=True)
class Upstream:
    """A single API upstream the pool can route to."""

    id: str
    base_url: str
    weight: float = 1.0
    headers: dict[str, str] = field(default_factory=dict)
    # outbound header name -> environment variable name holding its value
    header_env: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = 60.0

    def resolve_headers(self) -> dict[str, str]:
        """Build the outbound header set, resolving ``header_env`` from os.environ.

        Static ``headers`` are applied first, then ``header_env`` (env wins on
        conflict). Env vars that are unset are skipped. Secret *values* live only
        in the returned dict and are never logged by this module.
        """
        resolved: dict[str, str] = dict(self.headers)
        for header_name, env_name in self.header_env.items():
            value = os.environ.get(env_name)
            if value is not None:
                resolved[header_name] = value
        return resolved

    def missing_env(self) -> list[str]:
        """Return env var names referenced by ``header_env`` that are unset."""
        return [env for env in self.header_env.values() if env not in os.environ]

    def summary(self) -> dict[str, object]:
        """Secret-safe description: keys and env var *names* only, no values."""
        return {
            "id": self.id,
            "base_url": self.base_url,
            "weight": self.weight,
            "timeout_seconds": self.timeout_seconds,
            "static_header_keys": sorted(self.headers.keys()),
            "header_env": {
                header: env_name for header, env_name in sorted(self.header_env.items())
            },
            "missing_env": self.missing_env(),
        }


@dataclass(frozen=True)
class PoolConfig:
    """Pool-wide failover defaults."""

    retry_statuses: frozenset[int] = frozenset({429, 500, 502, 503, 504})
    max_attempts: int = 3
    cooldown_seconds: float = 30.0


@dataclass(frozen=True)
class AppConfig:
    """Top-level config: the pool defaults plus the upstream list."""

    upstreams: tuple[Upstream, ...]
    pool: PoolConfig = field(default_factory=PoolConfig)

    def summary(self) -> dict[str, object]:
        return {
            "pool": {
                "retry_statuses": sorted(self.pool.retry_statuses),
                "max_attempts": self.pool.max_attempts,
                "cooldown_seconds": self.pool.cooldown_seconds,
            },
            "upstreams": [u.summary() for u in self.upstreams],
        }


def _coerce_headers(raw: object, *, where: str) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a table of string -> string")
    out: dict[str, str] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise ConfigError(f"{where} keys and values must be strings")
        out[k] = v
    return out


def _parse_upstream(raw: object, index: int) -> Upstream:
    if not isinstance(raw, dict):
        raise ConfigError(f"upstream #{index} must be a table")

    uid = raw.get("id")
    if not isinstance(uid, str) or not uid.strip():
        raise ConfigError(f"upstream #{index} is missing a non-empty string 'id'")

    base_url = raw.get("base_url")
    if not isinstance(base_url, str) or not base_url.strip():
        raise ConfigError(f"upstream '{uid}' is missing a non-empty 'base_url'")
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfigError(
            f"upstream '{uid}' base_url must be an http(s) URL, got {base_url!r}"
        )

    weight = raw.get("weight", 1.0)
    if not isinstance(weight, (int, float)) or isinstance(weight, bool) or weight <= 0:
        raise ConfigError(f"upstream '{uid}' weight must be a positive number")

    timeout = raw.get("timeout_seconds", 60.0)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise ConfigError(f"upstream '{uid}' timeout_seconds must be a positive number")

    return Upstream(
        id=uid,
        base_url=base_url.rstrip("/"),
        weight=float(weight),
        headers=_coerce_headers(raw.get("headers"), where=f"upstream '{uid}' headers"),
        header_env=_coerce_headers(
            raw.get("header_env"), where=f"upstream '{uid}' header_env"
        ),
        timeout_seconds=float(timeout),
    )


def _parse_pool(raw: object) -> PoolConfig:
    if raw is None:
        return PoolConfig()
    if not isinstance(raw, dict):
        raise ConfigError("[pool] must be a table")

    defaults = PoolConfig()

    statuses = raw.get("retry_statuses", sorted(defaults.retry_statuses))
    if not isinstance(statuses, list) or not all(
        isinstance(s, int) and not isinstance(s, bool) for s in statuses
    ):
        raise ConfigError("pool.retry_statuses must be a list of integers")

    max_attempts = raw.get("max_attempts", defaults.max_attempts)
    if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or max_attempts < 1:
        raise ConfigError("pool.max_attempts must be an integer >= 1")

    cooldown = raw.get("cooldown_seconds", defaults.cooldown_seconds)
    if not isinstance(cooldown, (int, float)) or isinstance(cooldown, bool) or cooldown < 0:
        raise ConfigError("pool.cooldown_seconds must be a number >= 0")

    return PoolConfig(
        retry_statuses=frozenset(statuses),
        max_attempts=max_attempts,
        cooldown_seconds=float(cooldown),
    )


def parse_config(data: dict[str, object]) -> AppConfig:
    """Build and validate an :class:`AppConfig` from already-parsed TOML data."""
    raw_upstreams = data.get("upstream")
    if not isinstance(raw_upstreams, list) or not raw_upstreams:
        raise ConfigError("config must define at least one [[upstream]]")

    upstreams = tuple(
        _parse_upstream(item, i) for i, item in enumerate(raw_upstreams)
    )

    seen: set[str] = set()
    for u in upstreams:
        if u.id in seen:
            raise ConfigError(f"duplicate upstream id: {u.id!r}")
        seen.add(u.id)

    return AppConfig(upstreams=upstreams, pool=_parse_pool(data.get("pool")))


def load_config(path: str | os.PathLike[str]) -> AppConfig:
    """Read and validate a TOML config file from ``path``."""
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"config file not found: {p}")
    try:
        with p.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {p}: {exc}") from exc
    return parse_config(data)
