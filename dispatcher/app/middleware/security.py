"""Security response headers and CSRF protection middleware."""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp
from urllib.parse import urlparse

from ..config import settings

logger = logging.getLogger(__name__)

# ─── Security Headers ────────────────────────────────────────────

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "frame-ancestors 'none'; "
        "connect-src 'self'; "
        "img-src 'self' data:; "
        "style-src 'self'; "
        "form-action 'self'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-XSS-Protection": "0",
}

COMPUTE_CSRF_EXEMPT = {
    "/api/v1/compute/register",
    "/api/v1/compute/heartbeat",
    "/api/v1/compute/tasks/pull",
}

_COMPUTE_TASK_ACTIONS = {"renew", "log", "finish", "fail"}

SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


def add_security_headers(response: Response) -> Response:
    """Add security headers to *any* response — 2xx, 4xx, 5xx alike."""
    for header, value in SECURITY_HEADERS.items():
        response.headers[header] = value
    return response


def _build_origin_pattern(origins: list[str], allow_localhost: bool = True) -> re.Pattern:
    """Build a strict regex from a list of allowed origin URLs."""
    parts = []
    for origin in origins:
        o = origin.strip().rstrip("/")
        if o:
            parts.append("^" + re.escape(o) + "$")
    if allow_localhost:
        parts.append(r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$")
    return re.compile("|".join(parts)) if parts else re.compile(r"$^")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to every response."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        response = await call_next(request)
        return add_security_headers(response)


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(48)


def csrf_token_hmac(session_id: str, secret: str) -> str:
    return hmac.new(
        secret.encode(),
        session_id.encode(),
        hashlib.sha256,
    ).hexdigest()


def _is_compute_task_path(path: str) -> bool:
    """True if ``/api/v1/compute/tasks/{id}/{action}`` for compute actions.
    Does NOT match ``/api/v1/client/tasks/{id}/cancel`` (len differs)."""
    parts = path.strip("/").split("/")
    return (
        len(parts) == 6
        and parts[0] == "api"
        and parts[1] == "v1"
        and parts[2] == "compute"
        and parts[3] == "tasks"
        and parts[5] in _COMPUTE_TASK_ACTIONS
    )


class CSRFMiddleware(BaseHTTPMiddleware):
    """CSRF protection for session-cookie-authenticated requests.

    Origin header takes precedence.  If absent, Referer is parsed with
    ``urllib.parse.urlparse`` to extract ``scheme://netloc`` for comparison.
    Security headers are added to CSRF failure responses.
    """

    def __init__(self, app: ASGIApp, csrf_secret: str = "change-me-csrf"):
        super().__init__(app)
        self.csrf_secret = csrf_secret

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if request.method in SAFE_METHODS:
            return await call_next(request)

        path = request.url.path.rstrip("/")

        if path in COMPUTE_CSRF_EXEMPT:
            return await call_next(request)
        if _is_compute_task_path(path):
            return await call_next(request)

        session_id = request.cookies.get("dispatch_session")
        if not session_id:
            return await call_next(request)

        # Validate X-CSRF-Token
        csrf_header = request.headers.get("X-CSRF-Token", "")
        expected_csrf = csrf_token_hmac(session_id, self.csrf_secret)
        if not csrf_header or not hmac.compare_digest(csrf_header, expected_csrf):
            logger.warning("CSRF check failed: %s %s", request.method, path)
            return add_security_headers(Response(status_code=403, content="CSRF validation failed"))

        # Origin / Referer check
        origin = request.headers.get("Origin", "")
        referer = request.headers.get("Referer", "")
        allowed_re = _build_origin_pattern(settings.csrf_origins_list, settings.csrf_localhost_dev)

        if origin:
            if not allowed_re.match(origin):
                logger.warning("CSRF origin rejected: %s", origin)
                return add_security_headers(Response(status_code=403, content="Origin not allowed"))
        elif referer:
            # Parse Referer to get scheme://netloc only
            try:
                parsed = urlparse(referer)
                ref_origin = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
            except Exception:
                ref_origin = ""
            if not ref_origin or not allowed_re.match(ref_origin):
                logger.warning("CSRF referer rejected: %s (origin: %s)", referer, ref_origin)
                return add_security_headers(Response(status_code=403, content="Referer not allowed"))

        return await call_next(request)
