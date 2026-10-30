"""In-process sliding-window rate limiting.

Limits are per replica (MOKS runs 1-2 API replicas). They protect login and expensive
endpoints from bursts; Traefik can add a cluster-wide RateLimit middleware if needed.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import HTTPException


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, limit: int, window: float = 60.0) -> None:
        now = time.monotonic()
        q = self._hits[key]
        while q and q[0] <= now - window:
            q.popleft()
        if len(q) >= limit:
            retry = max(1, int(window - (now - q[0])))
            raise HTTPException(429, "Too many requests; slow down.", headers={"Retry-After": str(retry)})
        q.append(now)
        if len(self._hits) > 50_000:  # bound memory under key-spraying
            for k in list(self._hits)[:10_000]:
                if not self._hits[k] or self._hits[k][-1] <= now - window:
                    del self._hits[k]


limiter = RateLimiter()
