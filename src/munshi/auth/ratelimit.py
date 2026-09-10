"""A small in-process token bucket, keyed by client address, for the two
endpoints an attacker would hammer: sign-in and sign-up. Good for one
process; put a real limiter (or Caddy's) in front for a fleet."""
from __future__ import annotations

import threading
import time


class RateLimiter:
    def __init__(self, rate_per_minute: int = 10, burst: int = 10) -> None:
        self.rate = rate_per_minute / 60.0
        self.burst = burst
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get(key, (float(self.burst), now))
            tokens = min(self.burst, tokens + (now - last) * self.rate)
            if tokens < 1:
                self._buckets[key] = (tokens, now)
                return False
            self._buckets[key] = (tokens - 1, now)
            if len(self._buckets) > 10_000:     # forget the oldest on a flood
                for k in list(self._buckets)[:5_000]:
                    self._buckets.pop(k, None)
            return True
