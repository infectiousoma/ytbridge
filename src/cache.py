import json
import redis
from . import config

_rds = redis.Redis.from_url(config.REDIS_URL, decode_responses=True)

def cache_get(key: str):
    try:
        return _rds.get(key)
    except Exception:
        return None

def cache_set(key: str, value: str, ttl: int = config.REDIS_TTL):
    try:
        _rds.setex(key, ttl, value)
    except Exception:
        pass

def cache_get_json(key: str):
    raw = cache_get(key)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None

def cache_set_json(key: str, obj, ttl: int = config.REDIS_TTL):
    try:
        cache_set(key, json.dumps(obj), ttl)
    except Exception:
        pass
