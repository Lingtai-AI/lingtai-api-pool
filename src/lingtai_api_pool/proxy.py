"""Starlette proxy application.

Generic pass-through: forwards method/path/query/body to a selected upstream and
relays the response. Selection is sticky-key aware (rendezvous hashing) with a
least-in-flight fallback, and failover retries other upstreams on retryable
status codes, timeouts, and connection errors before any response bytes reach
the client.
"""

from __future__ import annotations

import contextlib
import json
from typing import Optional

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .config import AppConfig, Upstream
from .pool import UpstreamPool
from .routing import extract_sticky_key, select_least_inflight, select_sticky

# Hop-by-hop headers must not be forwarded (RFC 7230 6.1). "host" is dropped so
# httpx sets it from the upstream URL; "content-length" is recomputed by httpx.
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)

# Additional response headers to drop: we relay the *decoded* body (httpx
# transparently decodes content-encoding), so forwarding the original
# content-encoding/length would mislead the client about the bytes it receives.
_RESPONSE_DROP = _HOP_BY_HOP | {"content-encoding"}


def _filter_headers(
    headers: list[tuple[bytes, bytes]],
    drop: frozenset[str] = _HOP_BY_HOP,
) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw_name, raw_value in headers:
        name = raw_name.decode("latin-1")
        if name.lower() in drop:
            continue
        out[name] = raw_value.decode("latin-1")
    return out


def _wants_stream(body: bytes | None) -> bool:
    if not body:
        return False
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return False
    return isinstance(parsed, dict) and parsed.get("stream") is True


def _build_outbound_headers(request: Request, upstream: Upstream) -> dict[str, str]:
    """Client headers (hop-by-hop stripped) overlaid with per-upstream headers."""
    headers = _filter_headers(request.headers.raw)
    # Per-upstream static + env-sourced headers win over inbound ones. Secret
    # values come from resolve_headers() and are never logged here.
    for name, value in upstream.resolve_headers().items():
        headers[name] = value
    return headers


def _is_retryable_status(status: int, config: AppConfig) -> bool:
    return status in config.pool.retry_statuses


class ProxyApp:
    """Holds shared state (config, pool, httpx client) for the Starlette app."""

    def __init__(
        self,
        config: AppConfig,
        *,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.config = config
        self.pool = UpstreamPool(config)
        # A shared client; for tests an httpx.AsyncClient(transport=MockTransport)
        # can be injected so no real network is used.
        self._client = client
        self._owns_client = client is None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient()
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    def _select(self, sticky_key: Optional[str], candidates: list[Upstream]) -> Optional[Upstream]:
        if sticky_key is not None:
            return select_sticky(sticky_key, candidates)
        in_flight = {u.id: self.pool.in_flight(u.id) for u in candidates}
        return select_least_inflight(candidates, in_flight)

    async def handle(self, request: Request) -> Response:
        body = await request.body()
        sticky_key = extract_sticky_key(request.headers, body)
        stream = _wants_stream(body)

        attempted: set[str] = set()
        last_error: Optional[Response] = None

        for _ in range(self.config.pool.max_attempts):
            candidates = [
                u for u in self.pool.healthy() if u.id not in attempted
            ]
            if not candidates:
                break

            upstream = self._select(sticky_key, candidates)
            if upstream is None:
                break
            attempted.add(upstream.id)

            result = await self._forward(request, upstream, body, stream)
            if result is not None:
                return result
            # _forward returned None -> upstream failed and was put on cooldown.
            last_error = JSONResponse(
                {"error": f"upstream {upstream.id!r} failed"}, status_code=502
            )

        if not self.pool.healthy() and last_error is None:
            return JSONResponse(
                {"error": "no healthy upstreams available"}, status_code=503
            )
        return last_error or JSONResponse(
            {"error": "all upstreams exhausted"}, status_code=502
        )

    async def _forward(
        self,
        request: Request,
        upstream: Upstream,
        body: bytes,
        stream: bool,
    ) -> Optional[Response]:
        """Forward to one upstream. Returns a Response on success, or None if the
        upstream failed in a retryable way (already marked on cooldown)."""
        url = httpx.URL(upstream.base_url + request.url.path)
        if request.url.query:
            url = url.copy_with(query=request.url.query.encode("ascii"))
        headers = _build_outbound_headers(request, upstream)
        req = self.client.build_request(
            request.method,
            url,
            headers=headers,
            content=body or None,
            timeout=upstream.timeout_seconds,
        )

        self.pool.acquire(upstream.id)
        try:
            response = await self.client.send(req, stream=True)
        except (httpx.TimeoutException, httpx.TransportError):
            self.pool.mark_failed(upstream.id)
            self.pool.release(upstream.id)
            return None

        if _is_retryable_status(response.status_code, self.config):
            await response.aclose()
            self.pool.mark_failed(upstream.id)
            self.pool.release(upstream.id)
            return None

        return await self._relay(response, upstream, stream)

    async def _relay(
        self, response: httpx.Response, upstream: Upstream, stream: bool
    ) -> Response:
        out_headers = _filter_headers(
            [(k.encode("latin-1"), v.encode("latin-1")) for k, v in response.headers.items()],
            drop=_RESPONSE_DROP,
        )
        content_type = response.headers.get("content-type", "")
        is_sse = content_type.startswith("text/event-stream")

        if stream or is_sse:
            async def body_iter():
                try:
                    async for chunk in response.aiter_bytes():
                        yield chunk
                finally:
                    await response.aclose()
                    self.pool.release(upstream.id)

            return StreamingResponse(
                body_iter(),
                status_code=response.status_code,
                headers=out_headers,
                media_type=content_type or None,
            )

        try:
            raw = await response.aread()
        finally:
            await response.aclose()
            self.pool.release(upstream.id)
        return Response(
            content=raw,
            status_code=response.status_code,
            headers=out_headers,
            media_type=content_type or None,
        )


def create_app(
    config: AppConfig,
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> Starlette:
    """Build the Starlette app. Pass ``client`` (e.g. with MockTransport) for tests."""
    proxy = ProxyApp(config, client=client)

    async def catch_all(request: Request) -> Response:
        return await proxy.handle(request)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette):
        try:
            yield
        finally:
            await proxy.aclose()

    app = Starlette(
        routes=[
            Route(
                "/{path:path}",
                catch_all,
                methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
            )
        ],
        lifespan=lifespan,
    )
    # expose for tests / introspection
    app.state.proxy = proxy
    return app
