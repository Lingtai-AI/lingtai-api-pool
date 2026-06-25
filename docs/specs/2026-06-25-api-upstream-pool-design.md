# lingtai-api-pool — Design (MVP v0.1)

## Purpose

A local HTTP server that acts as a **generic API upstream pool / virtual REST proxy**
for LingTai-compatible clients. Clients point their `base_url` at this server; the
server forwards requests to one of several configured upstreams, providing sticky
session routing and failover.

The first concrete target is proxying the OpenAI/Codex **Responses API**
(`POST /v1/responses`) across multiple OAuth-backed/API accounts, but the
abstractions are deliberately generic: an "upstream" is any HTTP API base URL
with optional auth headers. **Not Codex-only, not OAuth-only.**

## Why not Codex-only

Clients differ only in which sticky-key fields they send and which path they call.
By keeping the proxy a generic pass-through (opaque method/path/query/body) plus a
pluggable sticky-key extractor, the same server serves Codex, LingTai, or any
OpenAI-like client without per-client code.

## Architecture

Stack: **uvicorn + Starlette + httpx** (async). Chosen for clean ASGI streaming of
`text/event-stream` and httpx's `MockTransport` for fully offline tests.

Modules under `src/lingtai_api_pool/`:

- `config.py` — load/validate TOML (`tomllib`), dataclasses for `Upstream`/`PoolConfig`/`AppConfig`, secret-safe summary.
- `routing.py` — sticky-key extraction, weighted rendezvous hashing, least-inflight fallback.
- `pool.py` — runtime upstream state: in-flight counts, cooldown tracking, health filter.
- `proxy.py` — Starlette app: build outbound request, strip hop-by-hop headers, apply per-upstream + env headers, forward via httpx, retry/failover, stream or buffer response.
- `cli.py` — argparse CLI: `serve`, `check`, `route`.
- `__main__.py` — `python -m lingtai_api_pool`.

## Data flow (request)

1. Read sticky key (order): header `session_id` → header `thread_id` →
   header `x-codex-window-id` → body `prompt_cache_key` → body `session_id` → none.
   (Body read once, buffered, reused for forwarding so SSE-bodied POSTs still work.)
2. Filter upstreams to **healthy** (not on cooldown).
3. Select:
   - sticky key present → **weighted rendezvous hashing** (`score = weight / -ln(h)` where
     `h = normalized blake2b(key + upstream_id)`), pick max score. Deterministic & stable.
   - no key → **least in-flight** (ties broken by higher weight, then id).
4. Build outbound request to `upstream.base_url` + path + query, hop-by-hop headers removed,
   per-upstream `headers` + `header_env` (env var → header value) applied.
5. Send via httpx with `upstream.timeout_seconds`.
6. On `429/500/502/503/504`, timeout, or connect error: mark upstream on cooldown,
   pick next healthy upstream, retry — up to `pool.max_attempts` total attempts.
   Failover only happens **before** any response bytes are streamed to the client.
7. Stream response if `text/event-stream` or request asked `stream: true`; else buffer.
   Preserve status, content-type, and body. Hop-by-hop headers stripped from response too.

## Config (TOML)

```toml
[pool]
retry_statuses = [429, 500, 502, 503, 504]
max_attempts = 3
cooldown_seconds = 30.0

[[upstream]]
id = "primary"
base_url = "https://api.example-a.com"
weight = 2
timeout_seconds = 60
headers = { "x-client" = "lingtai" }
header_env = { "authorization" = "UPSTREAM_A_AUTH" }

[[upstream]]
id = "secondary"
base_url = "https://api.example-b.com"
```

Validation: ≥1 upstream, unique ids, valid base_url, weight > 0. `header_env` values
are env var *names*; missing env vars are warned about, never their values logged.

## Error handling

- No healthy upstreams → `503` JSON error.
- All attempts exhausted → return last upstream's error response (or `502` on transport failure).
- Secrets: header values from `headers`/`header_env` are never logged; summary shows
  header *keys* and env var *names* only, never resolved values.

## Testing (offline, pytest + httpx.MockTransport)

- Config load/validate + secret-safe summary (no secret values present).
- Rendezvous: same key → same upstream; different keys can differ; deterministic.
- Sticky-key extraction across all header/body fields and precedence.
- Proxy forwards to selected upstream; preserves status/body/content-type.
- Failover on 503 and on connection error → succeeds on another upstream; cooldown set.
- CLI smoke: `check` prints summary (no secrets); `route` prints selected upstream id.

## Out of scope (YAGNI for v0.1)

Active health probes, metrics/dashboards, persistent state, auth on the proxy itself,
config hot-reload, circuit-breaker half-open logic beyond simple time cooldown.


Note: outbound URL is `upstream.base_url + incoming.path + incoming.query`; configure `base_url` as the upstream origin/root prefix unless you intentionally want a path prefix.
