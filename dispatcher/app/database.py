"""Async SQLAlchemy setup — only the dispatcher uses this.

Supports two modes:
  1. DATABASE_URL env var (preferred) — e.g. sqlite+aiosqlite:///test.db
  2. mysql_* fields (fallback) — builds a mysql+asyncmy URL
"""
from __future__ import annotations

from sqlalchemy import URL
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import settings


def _build_url() -> str | URL:
    """Return the database URL to use."""
    if settings.database_url:
        return settings.database_url
    # Fallback: build mysql URL from individual fields
    return URL.create(
        "mysql+asyncmy",
        username=settings.mysql_user,
        password=settings.mysql_password,
        host=settings.mysql_host,
        port=settings.mysql_port,
        database=settings.mysql_database,
    )


DATABASE_URL = _build_url()

# SQLite does not support pool_size / max_overflow
_is_sqlite = isinstance(DATABASE_URL, str) and "sqlite" in DATABASE_URL

if _is_sqlite:
    engine = create_async_engine(DATABASE_URL, echo=False)
else:
    engine = create_async_engine(DATABASE_URL, echo=False, pool_size=10, max_overflow=20)

async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db():
    """FastAPI dependency — yields an async DB session."""
    async with async_session_factory() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db():
    """Create all tables (idempotent)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
