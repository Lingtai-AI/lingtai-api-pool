"""Runtime upstream state: in-flight counts and cooldown / health tracking.

This is the mutable counterpart to the immutable :mod:`config` dataclasses. It is
deliberately small and synchronous; under asyncio the event loop serializes the
quick read/modify operations here, so no extra locking is needed for the MVP.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .config import AppConfig, Upstream


@dataclass
class _UpstreamState:
    upstream: Upstream
    in_flight: int = 0
    cooldown_until: float = 0.0


@dataclass
class UpstreamPool:
    """Tracks health and load for the configured upstreams."""

    config: AppConfig
    # injectable clock for deterministic tests
    clock: "callable[[], float]" = time.monotonic
    _states: dict[str, _UpstreamState] = field(init=False)

    def __post_init__(self) -> None:
        self._states = {
            u.id: _UpstreamState(upstream=u) for u in self.config.upstreams
        }

    def healthy(self) -> list[Upstream]:
        """Upstreams not currently on cooldown, in config order."""
        now = self.clock()
        return [
            s.upstream
            for s in self._states.values()
            if s.cooldown_until <= now
        ]

    def in_flight(self, upstream_id: str) -> int:
        return self._states[upstream_id].in_flight

    def acquire(self, upstream_id: str) -> None:
        self._states[upstream_id].in_flight += 1

    def release(self, upstream_id: str) -> None:
        state = self._states[upstream_id]
        if state.in_flight > 0:
            state.in_flight -= 1

    def mark_failed(self, upstream_id: str) -> None:
        """Put an upstream on cooldown for ``pool.cooldown_seconds``."""
        state = self._states[upstream_id]
        state.cooldown_until = self.clock() + self.config.pool.cooldown_seconds

    def is_on_cooldown(self, upstream_id: str) -> bool:
        return self._states[upstream_id].cooldown_until > self.clock()
