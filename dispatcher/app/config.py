"""Dispatcher configuration via environment variables."""
from __future__ import annotations

import logging
import os

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── Environment ──────────────────────────────────────────────────
    # "development" (default) or "production"
    # In production mode, default secrets cause a startup error.
    environment: str = "development"

    # ── Database ────────────────────────────────────────────────────
    database_url: str = ""

    # MySQL fallback (only used when DATABASE_URL is empty)
    mysql_host: str = "localhost"
    mysql_port: int = 3306
    mysql_user: str = "root"
    mysql_password: str = ""
    mysql_database: str = "wuzhu_dispatch"

    # ── Master secrets ──────────────────────────────────────────────
    dispatch_server_secret: str = "change-me-secret"
    registration_token: str = ""
    session_secret: str = "change-me-session-secret"

    # ── Session / Client auth ──────────────────────────────────────
    session_ttl_seconds: int = 3600
    bcrypt_rounds: int = 12

    # ── Server ──────────────────────────────────────────────────────
    host: str = "0.0.0.0"
    port: int = 8000

    # ── CORS / CSRF origins (comma-separated URLs) ──────────────────
    cors_allowed_origins: str = "https://admin.dispatch.example.com"
    csrf_allowed_origins: str = "https://admin.dispatch.example.com"
    csrf_localhost_dev: bool = True

    # ── Scheduler ───────────────────────────────────────────────────
    node_offline_seconds: int = 60
    task_lease_seconds: int = 300
    max_lease_seconds: int = 3600
    scheduler_interval_seconds: int = 30

    # ── Rate limiting (requests / window_seconds) ───────────────────
    rate_limit_login: int = 50
    rate_limit_login_window: int = 300
    rate_limit_heartbeat: int = 30
    rate_limit_heartbeat_window: int = 60
    rate_limit_pull: int = 20
    rate_limit_pull_window: int = 60
    rate_limit_log: int = 60
    rate_limit_log_window: int = 60
    rate_limit_renew: int = 30
    rate_limit_renew_window: int = 60
    rate_limit_task_create: int = 30
    rate_limit_task_create_window: int = 60

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @property
    def effective_registration_token(self) -> str:
        return self.registration_token or self.dispatch_server_secret

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    @property
    def csrf_origins_list(self) -> list[str]:
        return [o.strip() for o in self.csrf_allowed_origins.split(",") if o.strip()]


settings = Settings()


def check_production_settings():
    """Refuse to start if running in production with default secrets."""
    if settings.environment == "production":
        defaults = {
            "DISPATCH_SERVER_SECRET": settings.dispatch_server_secret,
            "SESSION_SECRET": settings.session_secret,
        }
        if settings.dispatch_server_secret == "change-me-secret":
            raise RuntimeError(
                "PRODUCTION BLOCKED: DISPATCH_SERVER_SECRET is still the default "
                "value 'change-me-secret'. Set a strong random secret in .env or "
                "the environment before running in production mode."
            )
        if settings.session_secret == "change-me-session-secret":
            raise RuntimeError(
                "PRODUCTION BLOCKED: SESSION_SECRET is still the default "
                "value 'change-me-session-secret'. Set a strong random secret "
                "in .env or the environment before running in production mode."
            )
        logger = logging.getLogger(__name__)
        logger.info("Production mode — secrets validated.")
