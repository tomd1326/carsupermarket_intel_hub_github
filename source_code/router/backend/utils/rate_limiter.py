import contextlib
import collections
import threading
import time
from typing import Dict, Optional, Type


class RateLimiter(contextlib.AbstractContextManager):
    """A leaky bucket rate limiter.
    Allows up to max_rate / time_period acquisitions before blocking.
    time_period is measured in seconds; the default is 60.
    """
    def __init__(
        self,
        rate_limit: float,
        time_window: float = 60
    ) -> None:
        self._max_level = rate_limit
        self._rate_per_sec = rate_limit / time_window
        self._level = 0.0
        self._last_check = time.time()
        self._waiters: Dict[int, threading.Event] = collections.OrderedDict()
        self._lock = threading.Lock()

    def _leak(self) -> None:
        """Drip out capacity from the bucket."""
        if self._level:
            elapsed = time.time() - self._last_check
            decrement = elapsed * self._rate_per_sec
            self._level = max(self._level - decrement, 0)
        self._last_check = time.time()

    def has_capacity(self, amount: float = 1) -> bool:
        """Check if there is enough space remaining in the bucket"""
        with self._lock:
            self._leak()
            requested = self._level + amount
            if requested < self._max_level:
                for event in self._waiters.values():
                    if not event.is_set():
                        event.set()
                        break
            return self._level + amount <= self._max_level

    def acquire(self, amount: float = 1) -> None:
        """Acquire space in the bucket.

        If the bucket is full, block until there is space.

        """
        if amount > self._max_level:
            raise ValueError("Can't acquire more than the bucket capacity")

        while not self.has_capacity(amount):
            event = threading.Event()
            thread_id = threading.get_ident()
            self._waiters[thread_id] = event
            event.wait(timeout=1 / self._rate_per_sec * amount)
            with self._lock:
                self._waiters.pop(thread_id, None)

        with self._lock:
            self._level += amount

    def __enter__(self) -> None:
        self.acquire()
        return None

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional
    ) -> None:
        return None
