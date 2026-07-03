"""Client API — /api/v1/client/*

Endpoints for task lifecycle from the client's perspective.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import ClientAuthContext, authenticate_client, require_client_role
from ..database import get_db
from ..models import Task
from ..schemas import TaskCreateRequest, TaskResponse
from ..services.audit_service import log_audit
from ..services.client_task_service import (
    cancel_task,
    create_task,
    get_task_by_id,
    get_task_logs,
    list_tasks_for_client,
    retry_task,
)

router = APIRouter(prefix="/api/v1/client", tags=["client"])


@router.post("/tasks", response_model=TaskResponse)
async def client_create_task(
    req: TaskCreateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx: ClientAuthContext = Depends(require_client_role("operator")),
):
    """Create a new task from the client.

    Shell/Hermes tasks require admin role.
    """
    execution = req.payload.get("execution", {})
    mode = execution.get("mode", req.payload.get("mode", ""))
    if mode in ("shell", "hermes"):
        if not ctx.is_system:
            from ..auth import _check_role
            if not _check_role("admin", ctx.role):
                raise HTTPException(
                    status_code=403,
                    detail="Shell/Hermes tasks require 'admin' role",
                )

    task = await create_task(
        db, req,
        created_by_user_id=ctx.user_id,
        created_by_client_token_id=ctx.token_id or ("system" if ctx.is_system else None),
    )

    await log_audit(
        db, f"task.create.{mode}", user_id=str(ctx.user_id or "system"),
        target_type="task", target_id=task.task_id,
        ip_address=request.client.host if request.client else None,
        detail={"type": req.type, "mode": mode, "priority": req.priority},
    )
    return TaskResponse.model_validate(task)


@router.get("/tasks", response_model=list[TaskResponse])
async def client_list_tasks(
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
    ctx: ClientAuthContext = Depends(authenticate_client),
):
    """List tasks visible to this client."""
    tasks = await list_tasks_for_client(db, ctx, status_filter=status)
    return [TaskResponse.model_validate(t) for t in tasks]


@router.get("/tasks/{task_id}", response_model=TaskResponse)
async def client_get_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    ctx: ClientAuthContext = Depends(authenticate_client),
):
    """Get task detail (scoped to client's own tasks unless admin)."""
    task = await get_task_by_id(db, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _check_task_ownership(ctx, task)
    return TaskResponse.model_validate(task)


@router.post("/tasks/{task_id}/cancel", response_model=TaskResponse)
async def client_cancel_task(
    task_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx: ClientAuthContext = Depends(require_client_role("operator")),
):
    """Cancel a task.

    Permissions checked BEFORE mutating: ownership verified first,
    then cancel performed.  This prevents operator A from modifying
    operator B's task even if the operation is rolled back.
    """
    task = await get_task_by_id(db, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _check_task_ownership(ctx, task)  # check BEFORE mutating
    task = await cancel_task(db, task_id)  # re-fetches inside, OK
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    await log_audit(
        db, "task.cancel", user_id=str(ctx.user_id or "system"),
        target_type="task", target_id=task_id,
        ip_address=request.client.host if request.client else None,
    )
    return TaskResponse.model_validate(task)


@router.post("/tasks/{task_id}/retry", response_model=TaskResponse)
async def client_retry_task(
    task_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx: ClientAuthContext = Depends(require_client_role("operator")),
):
    """Force retry a failed/cancelled/timeout task.

    Permissions checked BEFORE mutating.
    """
    task = await get_task_by_id(db, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _check_task_ownership(ctx, task)  # check BEFORE mutating
    task = await retry_task(db, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    await log_audit(
        db, "task.retry", user_id=str(ctx.user_id or "system"),
        target_type="task", target_id=task_id,
        ip_address=request.client.host if request.client else None,
    )
    return TaskResponse.model_validate(task)


@router.get("/tasks/{task_id}/logs")
async def client_get_logs(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    ctx: ClientAuthContext = Depends(authenticate_client),
):
    """Get log entries for a task."""
    task = await get_task_by_id(db, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _check_task_ownership(ctx, task)
    logs = await get_task_logs(db, task_id)
    return [
        {"log_time": l.log_time.isoformat(), "level": l.level, "message": l.message}
        for l in logs
    ]


# ── Artifact API ─────────────────────────────────────────────────────
# MVP 阶段暂不支持 artifact 文件下载，任务结果通过 result JSON 和 logs 查询。
# 后续版本通过 artifacts 表 + 安全路径映射 + Content-Disposition attachment 实现。


# ── Helpers ──────────────────────────────────────────────────────────


def _check_task_ownership(ctx: ClientAuthContext, task: Task):
    """Enforce visibility: system/admin/owner can see all; others see only their own."""
    if ctx.is_system:
        return
    if ctx.user:
        if ctx.role in ("admin", "owner"):
            return
        if task.created_by_user_id is None or task.created_by_user_id != ctx.user.user_id:
            raise HTTPException(status_code=403, detail="Not your task")
