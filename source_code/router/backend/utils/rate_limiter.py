from collections import deque
from threading import Lock
import datetime
import logging
import time

from utils.singleton import Singleton


logger = logging.getLogger(__name__)


class RateLimiter():
    def __init__(self, rate_limit: int, time_window: int):
        """
        Rate limiter class that limits the number of requests within a given time window.

        :param rate_limit: The maximum number of requests allowed in the time window.
        :param time_window: The time window in seconds within which the requests are counted.
        """
        self.rate_limit = rate_limit
        self.time_window = time_window
        self.lock = Lock()
        self.request_times = deque()  # Stores the times of the last 'rate_limit' requests

    def __enter__(self):
        with self.lock:
            current_time = datetime.datetime.now()
            # Clean up old entries that are outside the time window
            while self.request_times and (current_time - self.request_times[0]).total_seconds() > self.time_window:
                self.request_times.popleft()

            # Check if the current number of requests is within the rate limit
            if len(self.request_times) >= self.rate_limit:
                # Calculate the time to wait before allowing the next request
                next_allowed_time = self.request_times[0] + datetime.timedelta(seconds=self.time_window)
                sleep_time = (next_allowed_time - current_time).total_seconds()
                if sleep_time > 0:
                    logger.debug(f"Rate limit reached. Waiting {sleep_time} seconds.")
                    time.sleep(sleep_time)

            # Register the new request time
            self.request_times.append(current_time)

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass
