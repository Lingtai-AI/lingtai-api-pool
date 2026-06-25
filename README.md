# lingtai-api-pool

Local API upstream pool / virtual REST proxy for LingTai-compatible clients.

You run it on `127.0.0.1`, point a client's `base_url` at it, and it forwards each
request to one of several configured upstreams — providing **sticky session
routing** (the same session keeps hitting the same upstream) and **failover**
(a dead or rate-limited upstream is skipped automatically).

## Why not Codex-only

The first concrete target is the OpenAI/Codex **Responses API** (`POST /v1/responses`),
but nothing here is Codex- or OAuth-specific. An "upstream" is just an HTTP API
base URL with optional headers. The proxy forwards opaque method/path/query/body,
so the same server works for Codex, LingTai, or any OpenAI-like client. The only
client-specific knowledge is a small ordered list of sticky-key fields to look for.

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 1. validate config and see a secret-safe summary
lingtai-api-pool check --config examples/config.toml

# 2. see which upstream a session would route to
lingtai-api-pool route --config examples/config.toml --session-id my-session-123

# 3. provide upstream credentials via env, then serve
export PRIMARY_AUTH="Bearer sk-..."
export SECONDARY_AUTH="Bearer sk-..."
lingtai-api-pool serve --config examples/config.toml --host 127.0.0.1 --port 8765
```

## Configuration

TOML, read with the stdlib `tomllib`. See [`examples/config.toml`](examples/config.toml).

```toml
[pool]
retry_statuses = [429, 500, 502, 503, 504]  # statuses that trigger failover
max_attempts = 3                            # total attempts per client request
cooldown_seconds = 30.0                     # how long a failed upstream is skipped

[[upstream]]
id = "primary"                              # unique
base_url = "https://api.example.com"        # http(s)
weight = 2                                  # optional, default 1, must be > 0
timeout_seconds = 60                        # optional, default 60
headers = { "x-client" = "lingtai" }        # optional static headers
header_env = { "authorization" = "PRIMARY_AUTH" }  # outbound header -> env var name
```

**Secrets never live in config or logs.** `header_env` maps an outbound header to
the *name* of an environment variable; the value is read at request time. `check`
and the route summaries print header keys and env var names only, never values.
Configure auth on each upstream when credentials are per-account; if an upstream
has no configured auth header, any incoming client `authorization` header is
forwarded like an ordinary end-to-end header.

## Using with LingTai / Codex

Point the client's base URL at the local server. For a Codex Responses client,
set its API base to `http://127.0.0.1:8765` (so it calls
`http://127.0.0.1:8765/v1/responses`). The pool forwards to the selected upstream's
`base_url` plus the incoming path and query, then applies that upstream's configured
auth headers. In the common OpenAI-compatible case, keep upstream `base_url` at the
API origin/root prefix (for example `https://api.example.com`) so `/v1/responses`
is appended exactly once.

For the first Codex-pool use case, model each OAuth-backed/API account as one
`[[upstream]]`; keep its credential in an environment variable referenced by
`header_env`. The repo stays generic so later products can reuse the same routing
and failover layer without a Codex- or OAuth-specific rename.

## Route stickiness

The proxy extracts a sticky key from the request, in this order:

1. header `session_id`
2. header `thread_id`
3. header `x-codex-window-id`
4. JSON body `prompt_cache_key`
5. JSON body `session_id`
6. otherwise: no sticky key

The first two header names are literal underscore spellings (`session_id`,
`thread_id`) because LingTai's Codex REST adapter emits them that way; the MVP
does not alias hyphenated variants such as `Session-Id`.

With a sticky key, selection uses **weighted rendezvous hashing** over the healthy
upstreams: the same key maps to the same upstream deterministically, and when an
upstream goes on cooldown only the keys that hashed to it move elsewhere. Without a
sticky key, the pool picks the **least in-flight** healthy upstream (weighted).

You can preview routing without sending traffic:

```bash
lingtai-api-pool route --config examples/config.toml --session-id abc
```

## Failover caveats

- Failover only happens **before** any response bytes are streamed to the client.
  Once streaming has begun (SSE or `stream: true`), a mid-stream upstream failure
  is surfaced to the client as-is — it is not retried.
- Retries are bounded by `pool.max_attempts` and never reuse an upstream within a
  single request. If all healthy upstreams are exhausted you get a `502`; if none
  are healthy at all you get a `503`.
- Cooldown is a simple time window (`cooldown_seconds`), not an active health probe.
  An upstream becomes eligible again purely by the clock, regardless of whether it
  has actually recovered.

## Development

```bash
pip install -e ".[dev]"
pytest
```

Tests are fully offline — outbound HTTP is stubbed with `httpx.MockTransport`, so
no real upstream is ever contacted.
