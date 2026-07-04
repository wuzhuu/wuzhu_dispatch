#!/usr/bin/env python3
"""Initialize the database schema.

Usage:
    python scripts/init_db.py

Requires either:
  - DATABASE_URL env var (e.g. sqlite+aiosqlite:///test.db)
  - or MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DATABASE env vars

This script uses the dispatcher module, which is the ONLY
component that should connect to the database.
"""

import asyncio
import sys
from pathlib import Path

# Add dispatcher to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dispatcher"))

# Explicitly import models so all tables are registered with Base.metadata
import app.models  # noqa: F401 — registers all ORM models
from app.database import engine, Base
from app.config import settings
from sqlalchemy.engine import make_url


def safe_db_url(url: str) -> str:
    """Redact the password from a database URL for safe logging."""
    try:
        u = make_url(url)
        if u.password:
            u = u.set(password="***")
        return str(u)
    except Exception:
        return "<redacted database url>"


async def main():
    db_url = settings.database_url or \
        f"mysql+asyncmy://{settings.mysql_user}@{settings.mysql_host}:{settings.mysql_port}/{settings.mysql_database}"
    print(f"Connecting to database...")
    print(f"Tables: users, sessions, client_api_tokens, compute_nodes, "
          f"compute_node_status, tasks, task_logs, artifacts, audit_logs")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("All tables created successfully.")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
