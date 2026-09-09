from __future__ import annotations

import asyncio
import heapq
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional


@dataclass
class _Bucket:
    events: deque[float]
    window_seconds: int
    generation: int = 0


class SlidingWindowLimiter:
    def __init__(self, *, max_keys: int = 4096) -> None:
        if max_keys <= 0:
            raise ValueError("max_keys must be positive")
        self._max_keys = max_keys
        self._buckets: dict[str, _Bucket] = {}
        self._expirations: list[tuple[float, int, str]] = []
        self._lock: Optional[asyncio.Lock] = None

    @property
    def tracked_key_count(self) -> int:
        """Current bounded key cardinality, exposed for metrics and tests."""
        return len(self._buckets)

    def _schedule(self, key: str, bucket: _Bucket) -> None:
        bucket.generation += 1
        heapq.heappush(
            self._expirations,
            (
                bucket.events[0] + bucket.window_seconds,
                bucket.generation,
                key,
            ),
        )

    def _prune(self, now: float) -> None:
        while self._expirations and self._expirations[0][0] <= now:
            _, generation, key = heapq.heappop(self._expirations)
            bucket = self._buckets.get(key)
            if bucket is None or bucket.generation != generation:
                continue
            cutoff = now - bucket.window_seconds
            while bucket.events and bucket.events[0] <= cutoff:
                bucket.events.popleft()
            if not bucket.events:
                del self._buckets[key]
            else:
                self._schedule(key, bucket)

    async def allow(self, key: str, *, limit: int, window_seconds: int) -> bool:
        if limit <= 0 or window_seconds <= 0:
            raise ValueError("limit and window_seconds must be positive")
        now = time.monotonic()
        cutoff = now - window_seconds
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            self._prune(now)
            bucket = self._buckets.get(key)
            if bucket is None:
                if len(self._buckets) >= self._max_keys:
                    return False
                bucket = _Bucket(deque(), window_seconds)
                self._buckets[key] = bucket
            elif bucket.window_seconds != window_seconds:
                raise ValueError("a limiter key cannot change its window")
            while bucket.events and bucket.events[0] <= cutoff:
                bucket.events.popleft()
            if len(bucket.events) >= limit:
                return False
            bucket.events.append(now)
            self._schedule(key, bucket)
            return True
