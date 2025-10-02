import asyncio
import time
from typing import Optional

class AsyncTokenBucket:
    """
    Optimized async token bucket with batching support.
    
    Key improvements:
    - Batch token grants to reduce lock contention
    - Pre-computed wait times
    - Minimal lock hold duration
    """
    def __init__(self, rate: float, capacity: Optional[float] = None):
        self.rate = float(rate)
        self.capacity = float(capacity or rate)
        self.tokens = self.capacity
        self.ts = time.monotonic()
        self._lock = asyncio.Lock()
        # Optimization: pre-compute minimum wait to reduce attempts
        self._min_wait = 1.0 / self.rate if self.rate > 0 else 0.001

    async def acquire(self, cost: float = 1.0):
        """Acquire tokens with optimized fast-path."""
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self.ts
                self.ts = now
                # Refill tokens
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
                
                if self.tokens >= cost:
                    self.tokens -= cost
                    return
                
                # Calculate precise wait time
                need = cost - self.tokens
                wait = need / self.rate if self.rate > 0 else self._min_wait
            
            # Sleep outside lock - use adaptive wait
            await asyncio.sleep(min(wait * 0.95, 0.1))  # 95% of needed time, max 100ms

    async def acquire_many(self, cost: float = 1.0, count: int = 1):
        """
        Batch acquisition for multiple tokens at once.
        More efficient than multiple acquire() calls.
        """
        total_cost = cost * count
        
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self.ts
            self.ts = now
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            
            if self.tokens >= total_cost:
                self.tokens -= total_cost
                return 0.0
            
            # Return wait time for caller to handle
            need = total_cost - self.tokens
            return need / self.rate if self.rate > 0 else 0.0