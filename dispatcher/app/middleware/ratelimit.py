"""In-memory rate limiter (MVP only — replace with Redis for production)."""

import json
import logging
import time
from collections import defaultdict
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from .security import add_security_headers

logger = logging.getLogger(__name__)


class InMemoryRateLimiter:
    """Sliding-window rate limiter per (key, route) pair."""

    def __init__(self):
        self._buckets: dict[tuple[str, str], list[float]] = defaultdict(list)

    def check(self, key: str, route_key: str, max_requests: int, window_seconds: int) -> bool:
        now = time.time()
        bucket_key = (key, route_key)
        timestamps = self._buckets[bucket_key]
        cutoff = now - window_seconds
        self._buckets[bucket_key] = [t for t in timestamps if t > cutoff]
        if len(self._buckets[bucket_key]) >= max_requests:
            return False
        self._buckets[bucket_key].append(now)
        return True


_rate_limiter = InMemoryRateLimiter()


class RateLimitMiddleware(BaseHTTPMiddleware):
    RULES: dict[str, tuple[int, int]] = {}

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path
        method = request.method
        client_ip = request.client.host if request.client else "unknown"

        if method == "OPTIONS":
            return await call_next(request)

        rule_key = self._match_rule(path, method)
        if rule_key is None:
            return await call_next(request)

        max_req, window = self.RULES.get(rule_key, (0, 0))
        if max_req <= 0:
            return await call_next(request)

        key = self._rate_key(request, rule_key, client_ip)
        if not _rate_limiter.check(key, rule_key, max_req, window):
            logger.warning("Rate limited: %s %s from %s", method, path, client_ip)
            return add_security_headers(Response(
                status_code=429,
                content=json.dumps({"detail": "Too many requests"}),
                media_type="application/json",
            ))
        return await call_next(request)

    def _match_rule(self, path: str, method: str) -> str | None:
        if "/auth/login" in path:
            return "login"
        if "/compute/heartbeat" in path:
            return "heartbeat"
        if "/compute/tasks/pull" in path:
            return "pull"
        if "/compute/tasks/" in path and "/renew" in path:
            return "renew"
        if any(fp in path for fp in ("/log", "/finish", "/fail")):
            return "log"
        if method == "POST" and "/client/tasks" in path and "/pull" not in path:
            return "task_create"
        return None

    def _rate_key(self, request: Request, rule: str, ip: str) -> str:
        if rule == "login":
            return f"ip:{ip}"
        if rule in ("heartbeat", "pull", "log", "renew"):
            node_id = request.headers.get("X-Node-Id", "")
            return f"node:{node_id}" if node_id else f"ip:{ip}"
        if rule == "task_create":
            session = request.cookies.get("dispatch_session", "")
            return f"session:{session}" if session else f"ip:{ip}"
        return f"ip:{ip}"
