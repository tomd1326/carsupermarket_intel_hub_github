import asyncio
import time
from typing import Optional

class AsyncTokenBucket:
    """Async token bucket with steady refill. rate = tokens/sec."""
    def __init__(self, rate: float, capacity: Optional[float] = None):
        self.rate = float(rate)
        self.capacity = float(capacity or rate)
        self.tokens = self.capacity
        self.ts = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, cost: float = 1.0):
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self.ts
                self.ts = now
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
                if self.tokens >= cost:
                    self.tokens -= cost
                    return
                need = cost - self.tokens
                wait = need / self.rate if self.rate > 0 else 0.05
            await asyncio.sleep(min(max(wait, 0.001), 0.2))
