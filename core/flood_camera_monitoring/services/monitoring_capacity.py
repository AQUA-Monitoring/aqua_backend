"""Host telemetry and conservative admission control; no inference or API writes.

The dispatcher calls sample_capacity under its PostgreSQL advisory lock. /proc
must describe the worker host (the current single-host Docker deployment).
"""
import json
import time
from pathlib import Path

import redis
from django.conf import settings

KEY = 'flood:monitoring:capacity:v1'
GIB = 1024 ** 3


def enabled():
    return (getattr(settings, 'FLOOD_INTELLIGENCE_ENABLED', False)
            or getattr(settings, 'FLOOD_CAPACITY_CONTROL_ENABLED', False)
            or getattr(settings, 'FLOOD_MONITORING_POLICY', 'legacy') == 'conservative')


def unavailable():
    return {'status': 'UNAVAILABLE', 'limit_per_queue': 1, 'cpu_percent': None,
            'available_bytes': None, 'sampled_at': None}


def client():
    return redis.from_url(settings.REDIS_CACHE_URL, decode_responses=True,
                          socket_connect_timeout=1, socket_timeout=1)


def read_host():
    counters = [int(v) for v in Path('/proc/stat').read_text().splitlines()[0].split()[1:9]]
    memory = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
    return {'total': sum(counters), 'idle': counters[3],
            'available_bytes': int(memory['MemAvailable'].split()[0]) * 1024}


def advance(previous, reading, now):
    """Pure state transition. I/O wait counts as pressure, not idle capacity."""
    previous = previous or {}
    gap = now - previous.get('sampled_at', now)
    continuous = 0 < gap <= 30 and reading['total'] > previous.get('total', reading['total'])
    samples = previous.get('samples', []) if continuous else []
    if continuous:
        total = reading['total'] - previous['total']
        idle = reading['idle'] - previous['idle']
        samples = [*samples, {'at': now, 'seconds': gap,
                             'busy': max(0, min(100, 100 * (1 - idle / total)))}]
    samples = [s for s in samples if s['at'] > now - 60]
    span = sum(s['seconds'] for s in samples)
    cpu = sum(s['busy'] * s['seconds'] for s in samples) / span if span else None
    ready = span >= 59
    memory = reading['available_bytes']
    healthy = ready and cpu < 55 and memory > 3 * GIB
    healthy_since = previous.get('healthy_since') if continuous and healthy else None
    if healthy and healthy_since is None:
        healthy_since = now
    pressure = memory <= 2 * GIB or (cpu is not None and cpu >= 70)
    severe = memory <= GIB or (cpu is not None and cpu >= 90)
    was_restricted = previous.get('restricted', False) if continuous else True
    restricted = pressure or (was_restricted and not (healthy_since is not None and now - healthy_since >= 300))
    # Initial warmup does not require an additional cooldown; actual pressure does.
    if not previous or not continuous:
        restricted = pressure
    if severe:
        status, limit = 'SUSPENDED', 0
    elif not ready:
        status, limit = 'WARMUP', 1
    elif restricted:
        status, limit = 'LIMITED', 1
    else:
        status, limit = 'NORMAL', 2
    return {**reading, 'sampled_at': now, 'samples': samples,
            'cpu_percent': cpu, 'status': status, 'limit_per_queue': limit,
            'restricted': restricted, 'healthy_since': healthy_since}


def sample_capacity():
    if not enabled():
        return {'status': 'DISABLED', 'limit_per_queue': 2, 'cpu_percent': None,
                'available_bytes': None, 'sampled_at': None}
    try:
        cache = client()
        raw = cache.get(KEY)
        previous = json.loads(raw) if raw else None
        now = time.time()
        if previous and 0 <= now - previous['sampled_at'] < 10:
            return previous
        state = advance(previous, read_host(), now)
        cache.set(KEY, json.dumps(state), ex=600)
        return state
    except (OSError, ValueError, KeyError, TypeError, redis.RedisError):
        return unavailable()


def capacity_snapshot():
    """Read-only projection; never sample or reset cooldown during HTTP requests."""
    if not enabled():
        return {'status': 'DISABLED', 'limit_per_queue': 2, 'cpu_percent': None,
                'available_bytes': None, 'sampled_at': None}
    try:
        raw = client().get(KEY)
        state = json.loads(raw) if raw else None
        if not state or not 0 <= time.time() - state['sampled_at'] <= 30:
            return unavailable()
        return {k: state[k] for k in ('status', 'limit_per_queue', 'cpu_percent',
                                      'available_bytes', 'sampled_at')}
    except (OSError, ValueError, KeyError, TypeError, redis.RedisError):
        return unavailable()
