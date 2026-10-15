import json
import time
from typing import Any, Optional

import redis
from redis.backoff import NoBackoff
from redis.retry import Retry
from django.conf import settings


def get_redis() -> redis.Redis:
    return redis.from_url(settings.REDIS_CACHE_URL, decode_responses=True,
                          socket_connect_timeout=1, socket_timeout=2,
                          retry=Retry(NoBackoff(), 0))


def get_binary_redis() -> redis.Redis:
    """Return an isolated Redis client that preserves binary values."""
    return redis.from_url(settings.REDIS_CACHE_URL, decode_responses=False,
                          socket_connect_timeout=1, socket_timeout=2,
                          retry=Retry(NoBackoff(), 0))


def cache_set_json(key: str, value: Any, ex: Optional[int] = None) -> None:
    r = get_redis()
    r.set(key, json.dumps(value, default=str), ex=ex)


def cache_get_json(key: str) -> Optional[Any]:
    r = get_redis()
    raw = r.get(key)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def cache_delete(key: str) -> None:
    get_redis().delete(key)


def now_ts() -> float:
    return time.time()
