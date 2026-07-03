"""Background scheduler — lease expiry + offline detection.

Runs inside the dispatcher's uvicorn lifespan.
"""

from __future__ import annotations

import asyncio
import logging

from .config import settings
from .database import async_session_factory
from .services.compute_task_service import release_expired_leases
from .services.node_service import detect_offline_nodes

logger = logging.getLogger(__name__)


async def scheduler_loop():
    """Periodic sweep for expired leases and offline nodes."""
    logger.info(
        "Scheduler started (interval=%ds, node_offline=%ds, lease=%ds)",
        settings.scheduler_interval_seconds,
        settings.node_offline_seconds,
        settings.task_lease_seconds,
    )
    while True:
        try:
            async with async_session_factory() as db:
                await release_expired_leases(db)
                await detect_offline_nodes(db)
        except Exception:
            logger.exception("Scheduler tick error")
        await asyncio.sleep(settings.scheduler_interval_seconds)
