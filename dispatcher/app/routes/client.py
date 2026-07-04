"""Client API — /api/v1/client/*

Endpoints for task lifecycle from the client's perspective.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import (
    ClientAuthContext,
    authenticate_client,
    get_token_capabilities,
    require_client_role,
    validate_allowed_node_id,
    validate_can_target_node,
    validate_target_tags,
    validate_task_priority,
    validate_task_timeout,
    validate_template_allowed,
    validate_mode_allowed,
    validate_payload_size,
    validate_concurrent_tasks,
)
from ..database import get_db
from ..models import Task
from ..schemas import (
    QuickTaskRequest,
    QuickTaskResponse,
    TaskCreateRequest,
    TaskResponse,
    TargetSpec,
)
from ..services.audit_service import log_audit
from ..services.client_task_service import (
    cancel_task,
    create_task,
    get_task_by_id,
    get_task_logs,
    list_tasks_for_client,
    retry_task,
)
from ..services.compute_task_service import get_task_by_id as get_task_any
from ..templates import generate_task_payload, list_templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/client", tags=["client"])


# ── Template list ────────────────────────────────────────────────


@router.get("/templates")
async def client_list_templates():
    """List available task templates with their schemas."""
    return list_templates()


# ── Create task ──────────────────────────────────────────────────


@router.post("/tasks", response_model=TaskResponse)
async def client_create_task(
    req: TaskCreateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx: ClientAuthContext = Depends(require_client_role("operator")),
):
    """Create a new task.

    Two modes:
      1. **Template** — pass ``template_id`` + ``params`` (+ optional ``target``).
      2. **Direct** — pass ``type`` + ``payload`` (requires admin for shell/hermes).
    """
    caps = get_token_capabilities(ctx.token_scope)
    if req.template_id:
        return await _create_template_task(db, req, ctx, caps, request)
    return await _create_direct_task(db, req, ctx, caps, request)


# ── Quick task ───────────────────────────────────────────────────


@router.post("/tasks/quick", response_model=QuickTaskResponse)
async def client_quick_task(
    req: QuickTaskRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx: ClientAuthContext = Depends(require_client_role("operator")),
):
    """Create a task and wait briefly for its result.

    If *wait_seconds* exceeds the token's ``max_timeout_seconds`` limit,
    the request is **rejected with 403** (strict).
    """
    caps = get_token_capabilities(ctx.token_scope)
    token_max_wait = caps.get("max_timeout_seconds", 60)
    if req.wait_seconds > token_max_wait:
        raise HTTPException(
            status_code=403,
            detail=f"wait_seconds {req.wait_seconds} exceeds token limit {token_max_wait}",
        )

    create_req = TaskCreateRequest(
        template_id=req.template_id,
        params=req.params,
        target=req.target,
        priority=req.priority,
        timeout_seconds=req.timeout_seconds,
        max_retries=req.max_retries,
        requirements=req.requirements,
        payload=req.payload,
    )

    task = await _do_create(db, create_req, ctx, caps, request)
    task_id = task.task_id

    poll_interval = 0.5
    elapsed = 0.0
    max_wait = req.wait_seconds  # Use the request's value (already validated against limit)
    while elapsed < max_wait:
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
        current = await get_task_any(db, task_id)
        if current is None:
            break
        if current.status in ("success", "failed", "timeout", "cancelled"):
            return QuickTaskResponse(
                done=True,
                task_id=task_id,
                status=current.status,
                result=(current.result or {}) if current.status == "success" else None,
            )
        poll_interval = min(poll_interval * 1.5, 2.0)

    current = await get_task_any(db, task_id)
    status = current.status if current else "unknown"
    return QuickTaskResponse(
        done=False,
        task_id=task_id,
        status=status,
        retry_after_seconds=2,
    )


# ── Standard task endpoints ──────────────────────────────────────


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
    """Cancel a task."""
    task = await get_task_by_id(db, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _check_task_ownership(ctx, task)
    task = await cancel_task(db, task_id)
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
    """Force retry a failed/cancelled/timeout task."""
    task = await get_task_by_id(db, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _check_task_ownership(ctx, task)
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


# ── Internal helpers ─────────────────────────────────────────────


def _merge_target_into_requirements(target: TargetSpec, base_reqs: dict | None = None) -> dict:
    """Merge a ``TargetSpec`` into a requirements dict for scheduling.

    ``target.tags``        → ``requirements.required_tags``
    ``target.avoid_tags``  → ``requirements.avoid_tags``
    ``target.mode/node_id`` → ``requirements.target``
    ``target.requirements`` → deep-merged into the result
    """
    reqs = dict(base_reqs or {})

    if target.tags:
        existing = set(reqs.get("required_tags", []))
        existing.update(target.tags)
        reqs["required_tags"] = list(existing)

    if target.avoid_tags:
        existing = set(reqs.get("avoid_tags", []))
        existing.update(target.avoid_tags)
        reqs["avoid_tags"] = list(existing)

    # Store the target spec for node-specific scheduling
    reqs["target"] = {
        "mode": target.mode,
    }
    if target.node_id:
        reqs["target"]["node_id"] = target.node_id
    if target.tags:
        reqs["target"]["tags"] = target.tags
    if target.avoid_tags:
        reqs["target"]["avoid_tags"] = target.avoid_tags

    # Merge user-supplied requirements from target (deep merge dict fields)
    if target.requirements:
        for k, v in target.requirements.items():
            if k in ("required_tags", "avoid_tags") and isinstance(v, list):
                existing = set(reqs.get(k, []))
                existing.update(v)
                reqs[k] = list(existing)
            elif k == "runtime" and isinstance(v, dict):
                base = dict(reqs.get("runtime", {}))
                base.update(v)
                reqs["runtime"] = base
            elif isinstance(v, dict) and isinstance(reqs.get(k), dict):
                base = dict(reqs.get(k, {}))
                base.update(v)
                reqs[k] = base
            else:
                reqs[k] = v

    return reqs


async def _create_template_task(
    db: AsyncSession,
    req: TaskCreateRequest,
    ctx: ClientAuthContext,
    caps: dict,
    request: Request,
) -> Task:
    """Handle template-based task creation with scope enforcement."""
    template_id = req.template_id or ""

    from ..templates import get_template
    if not get_template(template_id):
        raise HTTPException(status_code=400, detail=f"Unknown template: {template_id!r}")

    # Scope: allowed_templates & mode=template
    validate_template_allowed(template_id, caps)
    validate_mode_allowed("template", caps)

    validate_task_priority(req.priority, caps)
    validate_task_timeout(req.timeout_seconds, caps)
    _validate_target(req.target, ctx, caps)

    # Generate payload from template (with caps for allow_internal_network)
    try:
        generated = generate_task_payload(template_id, req.params, caps=caps)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    final_payload = {**generated, **req.payload}
    final_payload["_template_id"] = template_id
    final_payload["_template_params"] = req.params

    # Payload size + concurrent limits
    validate_payload_size(final_payload, caps)
    await validate_concurrent_tasks(db, ctx, caps)

    # Merge target into requirements
    merged_reqs = _merge_target_into_requirements(req.target, req.requirements)

    create_payload = TaskCreateRequest(
        type=f"template:{template_id}",
        target=req.target,
        priority=req.priority,
        timeout_seconds=req.timeout_seconds,
        max_retries=req.max_retries,
        requirements=merged_reqs,
        payload=final_payload,
    )

    task = await create_task(
        db, create_payload,
        created_by_user_id=ctx.user_id,
        created_by_client_token_id=ctx.token_id or ("system" if ctx.is_system else None),
    )

    await log_audit(
        db, f"task.create.template:{template_id}",
        user_id=str(ctx.user_id or "system"),
        target_type="task", target_id=task.task_id,
        ip_address=request.client.host if request.client else None,
        detail={"template_id": template_id, "priority": req.priority},
    )
    return task


async def _create_direct_task(
    db: AsyncSession,
    req: TaskCreateRequest,
    ctx: ClientAuthContext,
    caps: dict,
    request: Request,
) -> Task:
    """Handle direct (non-template) task creation."""
    execution = req.payload.get("execution", {})
    mode = execution.get("mode", req.payload.get("mode", ""))

    # Mode scope validation (skip for system tokens or empty mode)
    if not ctx.is_system and mode:
        validate_mode_allowed(mode, caps)

    if mode in ("shell", "hermes"):
        if not ctx.is_system:
            from ..auth import _check_role
            if not _check_role("admin", ctx.role):
                raise HTTPException(
                    status_code=403,
                    detail="Shell/Hermes tasks require 'admin' role",
                )

    validate_task_priority(req.priority, caps)
    validate_task_timeout(req.timeout_seconds, caps)
    _validate_target(req.target, ctx, caps)

    # Payload size + concurrent limits
    validate_payload_size(req.payload or {}, caps)
    await validate_concurrent_tasks(db, ctx, caps)

    # Merge target into requirements
    merged_reqs = _merge_target_into_requirements(req.target, req.requirements)
    req.requirements = merged_reqs

    task = await create_task(
        db, req,
        created_by_user_id=ctx.user_id,
        created_by_client_token_id=ctx.token_id or ("system" if ctx.is_system else None),
    )

    await log_audit(
        db, f"task.create.{mode or 'direct'}",
        user_id=str(ctx.user_id or "system"),
        target_type="task", target_id=task.task_id,
        ip_address=request.client.host if request.client else None,
        detail={"type": req.type, "mode": mode, "priority": req.priority},
    )
    return task


async def _do_create(
    db: AsyncSession,
    req: TaskCreateRequest,
    ctx: ClientAuthContext,
    caps: dict,
    request: Request,
) -> Task:
    """Core create logic shared by POST /tasks and POST /tasks/quick."""
    if req.template_id:
        return await _create_template_task(db, req, ctx, caps, request)
    return await _create_direct_task(db, req, ctx, caps, request)


def _validate_target(target: TargetSpec, ctx: ClientAuthContext, caps: dict):
    """Validate a target spec against token capabilities."""
    if target.tags:
        validate_target_tags(target.tags, caps)
    if target.node_id:
        validate_can_target_node(ctx, caps)
        validate_allowed_node_id(target.node_id, caps)


def _check_task_ownership(ctx: ClientAuthContext, task: Task):
    """Enforce visibility: system/admin/owner can see all; others see only their own."""
    if ctx.is_system:
        return
    if ctx.user:
        if ctx.role in ("admin", "owner"):
            return
        if task.created_by_user_id is None or task.created_by_user_id != ctx.user.user_id:
            raise HTTPException(status_code=403, detail="Not your task")
