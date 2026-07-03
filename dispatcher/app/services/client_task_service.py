"""Client-facing task service.

Handles task creation, listing, cancellation, retry from the client's
perspective.  The key difference from compute_task_service is that
these methods handle ownership scoping (created_by_user_id).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import ClientAuthContext
from ..models import Task, TaskLog
from ..schemas import TaskCreateRequest


async def create_task(
    db: AsyncSession,
    req: TaskCreateRequest,
    created_by_user_id: str | None = None,
    created_by_client_token_id: str | None = None,
) -> Task:
    """Create a new task with ownership tracking."""
    task = Task(
        type=req.type,
        priority=req.priority,
        requirements=req.requirements,
        payload=req.payload,
        timeout_seconds=req.timeout_seconds,
        max_retries=req.max_retries,
        created_by_user_id=created_by_user_id,
        created_by_client_token_id=created_by_client_token_id,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return task


async def list_tasks_for_client(
    db: AsyncSession,
    ctx: ClientAuthContext,
    status_filter: str | None = None,
) -> list[Task]:
    """List tasks visible to this client.

    Admin/owner/system see all; viewer/operator see only their own.
    """
    query = select(Task).order_by(Task.created_at.desc())

    if status_filter:
        query = query.where(Task.status == status_filter)

    if not ctx.is_system and ctx.user and ctx.role in ("viewer", "operator"):
        query = query.where(Task.created_by_user_id == ctx.user.user_id)

    result = await db.execute(query)
    return list(result.scalars().all())


async def get_task_by_id(db: AsyncSession, task_id: str) -> Task | None:
    result = await db.execute(select(Task).where(Task.task_id == task_id))
    return result.scalar_one_or_none()


async def cancel_task(db: AsyncSession, task_id: str) -> Task | None:
    """Cancel a task (stop scheduling / abort)."""
    task = await get_task_by_id(db, task_id)
    if not task:
        return None
    if task.status in ("success", "failed", "cancelled"):
        return task
    task.status = "cancelled"
    task.finished_at = datetime.utcnow()
    await db.commit()
    return task


async def retry_task(db: AsyncSession, task_id: str) -> Task | None:
    """Force retry a failed/cancelled/timeout task."""
    task = await get_task_by_id(db, task_id)
    if not task:
        return None
    if task.status in ("success",):
        return task
    task.status = "pending"
    task.assigned_node_id = None
    task.lease_until = None
    task.started_at = None
    task.finished_at = None
    task.retry_count = 0
    await db.commit()
    return task


async def get_task_logs(db: AsyncSession, task_id: str) -> list[TaskLog]:
    result = await db.execute(
        select(TaskLog)
        .where(TaskLog.task_id == task_id)
        .order_by(TaskLog.log_time)
    )
    return list(result.scalars().all())
