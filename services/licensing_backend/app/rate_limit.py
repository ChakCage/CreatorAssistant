from __future__ import annotations

import time
from collections import defaultdict, deque

try:
    import redis
except ImportError:  # pragma: no cover - minimal local test installation
    redis = None


class RateLimiter:
    """Fixed-window limiter backed by Redis, with a local development fallback."""
    def __init__(self, redis_url: str = "") -> None:
        self.events: dict[str, deque[float]] = defaultdict(deque)
        self.redis = redis.Redis.from_url(redis_url, decode_responses=True) if redis_url and redis else None

    def allow(self, key: str, limit: int, window_seconds: int) -> bool:
        if self.redis is not None:
            bucket = int(time.time()) // window_seconds
            redis_key = f"creator-assistant:license-rate:{key}:{bucket}"
            try:
                with self.redis.pipeline() as pipe:
                    pipe.incr(redis_key)
                    pipe.expire(redis_key, window_seconds + 5)
                    count, _ = pipe.execute()
                return int(count) <= limit
            except Exception:
                # A Redis outage must not take the licensing API down. The
                # process-local limiter remains a conservative fallback.
                pass
        now = time.monotonic(); values = self.events[key]
        while values and values[0] <= now - window_seconds: values.popleft()
        if len(values) >= limit: return False
        values.append(now); return True
