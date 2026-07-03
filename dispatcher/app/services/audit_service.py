"""Audit logging service."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AuditLog

logger = logging.getLogger(__name__)


async def log_audit(
    db: AsyncSession,
    action: str,
    user_id: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    detail: dict[str, Any] | None = None,
) -> AuditLog:
    """Write an audit log entry."""
    entry = AuditLog(
        user_id=user_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        ip_address=ip_address,
        user_agent=user_agent,
        detail=detail,
        created_at=datetime.utcnow(),
    )
    db.add(entry)
    await db.commit()
    return entry


async def get_audit_logs(
    db: AsyncSession,
    limit: int = 100,
    offset: int = 0,
    action: str | None = None,
    user_id: str | None = None,
) -> tuple[list[AuditLog], int]:
    """Query audit logs with optional filters."""
    query = select(AuditLog)
    count_query = select(func.count(AuditLog.id))

    if action:
        query = query.where(AuditLog.action == action)
        count_query = count_query.where(AuditLog.action == action)
    if user_id:
        query = query.where(AuditLog.user_id == user_id)
        count_query = count_query.where(AuditLog.user_id == user_id)

    query = query.order_by(AuditLog.id.desc()).offset(offset).limit(limit)

    result = await db.execute(query)
    logs = list(result.scalars().all())

    count_result = await db.execute(count_query)
    total = count_result.scalar() or 0

    return logs, total
