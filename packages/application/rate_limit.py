"""Atomic Redis fixed windows; writes/login fail closed, reads have bounded fallback."""

import hashlib
import logging
import math
import threading
import time
import unicodedata

from redis.exceptions import RedisError

from packages.domain.security import ServiceError

COUNT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return {count, redis.call('TTL', KEYS[1])}
"""
RELEASE = """
local count = tonumber(redis.call('GET', KEYS[1]) or '0')
if count > 0 then return redis.call('DECR', KEYS[1]) end
return 0
"""


class RateLimits:
    def __init__(self, redis_client, *, clock=time.time):
        self.redis = redis_client
        self.clock = clock
        self._fallback = {}
        self._lock = threading.Lock()
        self._last_warning = None

    def ready(self):
        try:
            if not self.redis.ping():
                raise RedisError("Redis not ready")
        except RedisError:
            raise ServiceError("DEPENDENCY_UNAVAILABLE", 503, "Rate limiter unavailable") from None

    def _charge(self, category, identity, maximum, seconds, *, read=False):
        now = self.clock()
        window = int(now // seconds)
        remaining = max(1, math.ceil((window + 1) * seconds - now))
        digest = hashlib.sha256(identity.encode()).hexdigest()
        key = f"labsafe:limit:{category}:{digest}:{window}"
        try:
            count, ttl = self.redis.eval(COUNT, 1, key, remaining)
        except RedisError:
            if not read:
                raise ServiceError(
                    "DEPENDENCY_UNAVAILABLE", 503, "Rate limiter unavailable"
                ) from None
            with self._lock:
                self._fallback = {k: v for k, v in self._fallback.items() if v[1] > now}
                if self._last_warning != window:
                    logging.getLogger(__name__).warning(
                        "Redis unavailable; conservative local read limit"
                    )
                    self._last_warning = window
                if key not in self._fallback and len(self._fallback) >= 10000:
                    raise ServiceError(
                        "DEPENDENCY_UNAVAILABLE", 503, "Local rate limiter at capacity"
                    )
                count = self._fallback.get(key, (0, 0))[0] + 1
                self._fallback[key] = (count, now + remaining)
                maximum, ttl = min(maximum, 30), remaining
        if int(count) > maximum:
            raise ServiceError(
                "RATE_LIMITED", 429, "Too many requests", retry_after=max(1, int(ttl))
            )
        return key

    def login(self, tenant, username, ip):
        account = unicodedata.normalize("NFKC", username).strip().casefold()
        tenant = unicodedata.normalize("NFC", tenant).strip()
        # In-flight attempts reserve a slot; successful login releases only its slot.
        return self._charge("login", repr((tenant, account, ip)), 5, 900)

    def login_succeeded(self, ticket):
        try:
            self.redis.eval(RELEASE, 1, ticket)
        except RedisError:
            raise ServiceError("DEPENDENCY_UNAVAILABLE", 503, "Rate limiter unavailable") from None

    def user(self, principal, *, write):
        identity = f"{principal.tenant_id}:{principal.user_id}"
        self._charge("user", identity, 120, 60, read=not write)
        if write:
            self._charge("write", identity, 60, 60)

    def upload_grant(self, principal):
        self._charge("upload", f"{principal.tenant_id}:{principal.user_id}", 10, 60)
