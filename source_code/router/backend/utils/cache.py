from utils.singleton import Singleton
import functools
from inspect import signature
from abc import abstractmethod
from redis import Redis, ConnectionPool
import os
import json
import threading
import time


class CacheInterface(metaclass=Singleton):
    @abstractmethod
    def get(self, key):
        pass

    @abstractmethod
    def set(self, key, value, ex=None):
        pass


class RedisCache(CacheInterface):
    def __init__(self):
        redis_connection_string = os.environ.get('CACHE_CLUSTER', 'pricing-caching-system-aramisauto-terraform-dev.hlsycq.0001.use2.cache.amazonaws.com:6379')
        if redis_connection_string is None:
            raise ValueError("Redis connection string not set.")
        if not redis_connection_string.startswith('redis://'):
            redis_connection_string = f'redis://{redis_connection_string}'

        # Setup the connection pool
        self.pool = ConnectionPool.from_url(redis_connection_string, max_connections=10)

    def get(self, key):
        with Redis(connection_pool=self.pool) as conn:
            return conn.get(key)

    def set(self, key, value, ex=None):
        with Redis(connection_pool=self.pool) as conn:
            conn.set(key, value, ex)


class MemoryCache(CacheInterface):
    """In-process cache, honouring `ex` (seconds) the way Redis does.

    It used to ignore `ex`, so the ex=24h on the Autotrader calls did nothing
    and every response from a 5,000 vehicle run stayed in memory for the life
    of the process. One instance is shared by every decorated function and is
    hit from up to 40 worker threads at once, hence the lock.
    """

    # Expired entries are cleared out at most this often, on a set, so the
    # sweep does not run on every one of thousands of writes in a run.
    _SWEEP_SECONDS = 300

    def __init__(self):
        self.cache = {}  # key -> (value, expires_at or None)
        self._lock = threading.Lock()
        self._last_sweep = time.monotonic()

    def get(self, key):
        with self._lock:
            entry = self.cache.get(key)
            if entry is None:
                return None
            value, expires_at = entry
            if expires_at is not None and time.monotonic() >= expires_at:
                del self.cache[key]
                return None
            return value

    def set(self, key, value, ex=None):
        now = time.monotonic()
        with self._lock:
            self.cache[key] = (value, now + ex if ex else None)
            if now - self._last_sweep >= self._SWEEP_SECONDS:
                self._last_sweep = now
                expired = [k for k, (_, exp) in self.cache.items() if exp is not None and now >= exp]
                for k in expired:
                    del self.cache[k]


def cache(client_class: type = MemoryCache, ex=None, ignore_args=None):
    """
    Caching decorator that caches the return value of a function based on its arguments,
    excluding any arguments specified in ignore_args.

    Parameters:
    - client_class: The cache client class to use.
    - ex: The cache expiration time in seconds.
    - ignore_args: A list of arguments to ignore when caching.
    """
    if ignore_args is None:
        ignore_args = []
    if 'self' not in ignore_args:
        ignore_args.append('self')

    def decorator(func):
        sig = signature(func)
        client = client_class()

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # Bind the function arguments with the signature
            bound_arguments = sig.bind(*args, **kwargs)
            bound_arguments.apply_defaults()

            # Create cache key excluding specified arguments
            key_args = tuple(
                (name, bound_arguments.arguments[name])
                for name in bound_arguments.arguments
                if name not in ignore_args
            )

            key_kwargs = frozenset(
                (k, v) for k, v in kwargs.items() if k not in ignore_args
            )

            key = (key_args, key_kwargs)
            key_bytes = str(key).encode('utf-8')

            resp_bytes = client.get(key_bytes)
            if resp_bytes is not None:
                resp = json.loads(resp_bytes)
            else:
                resp = func(*args, **kwargs)
                resp_bytes = json.dumps(resp).encode('utf-8')
                client.set(key_bytes, resp_bytes, ex=ex)
            if ex is not None:
                # Update the expiration time
                client.set(key_bytes, resp_bytes, ex=ex)
            return resp

        return wrapper

    return decorator
