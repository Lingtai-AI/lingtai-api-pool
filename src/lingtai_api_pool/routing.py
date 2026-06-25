"""Sticky-key extraction and upstream selection.

Two selection strategies:

* **Weighted rendezvous hashing** for sticky keys — the same key deterministically
  maps to the same upstream as long as the healthy set is stable, and when an
  upstream drops out only the keys that hashed to it are redistributed.
* **Least in-flight** when there is no sticky key.

Selection is pure with respect to its inputs (the candidate list + per-candidate
load), which keeps it easy to test deterministically.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Iterable, Mapping, Optional

from .config import Upstream

# Header names checked, in priority order, before falling back to the JSON body.
STICKY_HEADER_ORDER = ("session_id", "thread_id", "x-codex-window-id")
# JSON body fields checked, in priority order.
STICKY_BODY_ORDER = ("prompt_cache_key", "session_id")


def extract_sticky_key(
    headers: Mapping[str, str],
    body: bytes | None,
) -> Optional[str]:
    """Extract the sticky routing key, or ``None`` if none is present.

    Order: header ``session_id`` -> header ``thread_id`` ->
    header ``x-codex-window-id`` -> body ``prompt_cache_key`` -> body ``session_id``.

    ``headers`` is expected to support case-insensitive lookup (Starlette's
    ``Headers`` does); a plain dict works if keys are already lowercase.
    """
    for name in STICKY_HEADER_ORDER:
        value = headers.get(name)
        if value:
            return value

    if body:
        try:
            parsed = json.loads(body)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            for field_name in STICKY_BODY_ORDER:
                value = parsed.get(field_name)
                if isinstance(value, str) and value:
                    return value

    return None


def _rendezvous_score(key: str, upstream: Upstream) -> float:
    """Weighted rendezvous (HRW) score for ``key`` on ``upstream``.

    Uses the standard ``weight / -ln(h)`` weighting where ``h`` is a hash of
    ``key|upstream_id`` normalized to ``(0, 1)``. Higher weight -> higher
    expected score -> larger share of keys, while any single key remains stable.
    """
    digest = hashlib.blake2b(
        f"{key}\x00{upstream.id}".encode("utf-8"), digest_size=8
    ).digest()
    # normalize to (0, 1); avoid exactly 0 or 1 for the log/division below
    raw = int.from_bytes(digest, "big")
    h = (raw + 1) / (2**64 + 1)
    return upstream.weight / -math.log(h)


def select_sticky(key: str, candidates: Iterable[Upstream]) -> Optional[Upstream]:
    """Pick the upstream with the highest rendezvous score for ``key``.

    Ties (extremely unlikely) break on upstream id for determinism.
    """
    best: Optional[Upstream] = None
    best_score = float("-inf")
    for upstream in candidates:
        score = _rendezvous_score(key, upstream)
        if score > best_score or (score == best_score and best is not None and upstream.id < best.id):
            best = upstream
            best_score = score
    return best


def select_least_inflight(
    candidates: Iterable[Upstream],
    in_flight: Mapping[str, int],
) -> Optional[Upstream]:
    """Pick the least-loaded healthy upstream.

    Load is in-flight count divided by weight (so a weight-2 upstream tolerates
    twice the load before being considered as busy). Ties break on higher weight,
    then upstream id, for determinism.
    """
    best: Optional[Upstream] = None
    best_key: tuple[float, float, str] | None = None
    for upstream in candidates:
        load = in_flight.get(upstream.id, 0) / upstream.weight
        # sort key: lower load first, then higher weight (negated), then id
        sort_key = (load, -upstream.weight, upstream.id)
        if best_key is None or sort_key < best_key:
            best = upstream
            best_key = sort_key
    return best
