"""Compute Server API — /api/v1/compute/*

Endpoints for compute-server lifecycle:
  POST /api/v1/compute/register    — Register/update static profile
  POST /api/v1/compute/heartbeat   — Report dynamic metrics
  POST /api/v1/compute/tasks/pull  — Pull an assigned task
  POST /api/v1/compute/tasks/{id}/renew — Extend lease
  POST /api/v1/compute/tasks/{id}/log   — Upload log entry
  POST /api/v1/compute/tasks/{id}/finish — Report success
  POST /api/v1/compute/tasks/{id}/fail   — Report failure

Authentication: X-Node-Id + Bearer <agent_token>
(registration uses registration_token separately)
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import verify_compute_node, verify_registration_token
from ..database import get_db
from ..models import ComputeNode, ComputeNodeStatus
from ..schemas import (
    HeartbeatRequest,
    MessageResponse,
    NodeRegisterRequest,
    TaskFailRequest,
    TaskFinishRequest,
    TaskLogRequest,
    TaskPullResponse,
    TaskRenewRequest,
)
from ..services.audit_service import log_audit
from ..services.compute_task_service import (
    append_task_log,
    fail_task,
    finish_task,
    pull_task_for_node,
    renew_task_lease,
)
from ..services.node_service import process_heartbeat, register_node

router = APIRouter(prefix="/api/v1/compute", tags=["compute"])


@router.post("/register", response_model=MessageResponse)
async def compute_register(
    req: NodeRegisterRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _ok: bool = Depends(verify_registration_token),
):
    """Register or update a compute server's static profile.

    Requires registration_token (defaults to DISPATCH_SERVER_SECRET).
    The agent_token in the body is stored as a SHA-256 hash.
    """
    node = await register_node(db, req)
    await log_audit(
        db, "compute.register", user_id=f"node:{req.node_id}",
        target_type="compute_node", target_id=req.node_id,
        ip_address=request.client.host if request.client else None,
        detail={"node_id": req.node_id, "name": req.name},
    )
    return MessageResponse(message=f"Compute node {req.node_id} registered")


@router.post("/heartbeat", response_model=MessageResponse)
async def compute_heartbeat(
    req: HeartbeatRequest,
    db: AsyncSession = Depends(get_db),
    node: ComputeNode = Depends(verify_compute_node),
):
    """Report dynamic metrics.  Uses X-Node-Id identity, ignores body.node_id."""
    await process_heartbeat(db, node, req)
    return MessageResponse(message="OK")


@router.post("/tasks/pull", response_model=TaskPullResponse | dict | None)
async def compute_pull_task(
    wait_seconds: int = 0,
    db: AsyncSession = Depends(get_db),
    node: ComputeNode = Depends(verify_compute_node),
):
    """Pull an available task for this compute server.

    Supports long polling:
    - ``?wait_seconds=25`` — block up to 25 s waiting for a task.
    - If a task becomes available during the wait, it is returned immediately.
    - On timeout returns ``{"task": null, "retry_after_seconds": 10}``.

    Atomic claim — returns None (no wait) if no matching task.
    """
    task, lease_duration = await pull_task_for_node(db, node)
    if task is not None:
        return TaskPullResponse(
            task_id=task.task_id,
            type=task.type,
            payload=task.payload,
            lease_until=task.lease_until,
            lease_seconds=lease_duration,
            timeout_seconds=task.timeout_seconds,
            max_retries=task.max_retries,
            retry_count=task.retry_count,
        )

    # Long polling: poll DB for up to wait_seconds
    if wait_seconds > 0:
        import asyncio
        import time
        deadline = time.time() + min(wait_seconds, 60)
        while time.time() < deadline:
            await asyncio.sleep(1.5)
            task, lease_duration = await pull_task_for_node(db, node)
            if task is not None:
                return TaskPullResponse(
                    task_id=task.task_id,
                    type=task.type,
                    payload=task.payload,
                    lease_until=task.lease_until,
                    lease_seconds=lease_duration,
                    timeout_seconds=task.timeout_seconds,
                    max_retries=task.max_retries,
                    retry_count=task.retry_count,
                )
        # Timeout — tell client when to retry
        return {"task": None, "retry_after_seconds": 10}

    return None


@router.post("/tasks/{task_id}/renew", response_model=MessageResponse)
async def compute_renew_lease(
    task_id: str,
    req: TaskRenewRequest,
    db: AsyncSession = Depends(get_db),
    node: ComputeNode = Depends(verify_compute_node),
):
    """Extend the lease on a running task."""
    await renew_task_lease(db, task_id, node.node_id, req)
    return MessageResponse(message="Lease renewed")


@router.post("/tasks/{task_id}/log", response_model=MessageResponse)
async def compute_upload_log(
    task_id: str,
    req: TaskLogRequest,
    db: AsyncSession = Depends(get_db),
    node: ComputeNode = Depends(verify_compute_node),
):
    """Append a log entry for a task (authenticated as compute node)."""
    await append_task_log(db, task_id, node.node_id, req)
    return MessageResponse(message="Log recorded")


@router.post("/tasks/{task_id}/finish", response_model=MessageResponse)
async def compute_finish_task(
    task_id: str,
    req: TaskFinishRequest,
    db: AsyncSession = Depends(get_db),
    node: ComputeNode = Depends(verify_compute_node),
):
    """Report task completion with result."""
    try:
        await finish_task(db, task_id, node.node_id, req)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return MessageResponse(message="Task finished")


@router.post("/tasks/{task_id}/fail", response_model=MessageResponse)
async def compute_fail_task(
    task_id: str,
    req: TaskFailRequest,
    db: AsyncSession = Depends(get_db),
    node: ComputeNode = Depends(verify_compute_node),
):
    """Report task failure.  Dispatcher may retry."""
    try:
        await fail_task(db, task_id, node.node_id, req)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return MessageResponse(message="Task failed")
