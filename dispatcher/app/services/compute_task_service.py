"""Compute-task lifecycle service.

This is what compute-server talks to.  It handles:
- Task pull (atomic claim with FOR UPDATE row lock)
- Lease renewal
- Log upload
- Finish / fail reporting
- Lease expiry release (background scheduler)

Key design for concurrent safety:
  pull_task_for_node uses SELECT ... FOR UPDATE on the node row and
  runs the entire claim in one transaction, so concurrent pulls from
  the same node cannot exceed max_parallel_tasks.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select, update as sql_update

# Import for_for_update if available
try:
    from sqlalchemy.dialects.mysql import insert as mysql_insert
    HAS_FOR_UPDATE = True
except ImportError:
    HAS_FOR_UPDATE = False

from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import ComputeNode, ComputeNodeStatus, Task, TaskLog
from ..schemas import (
    TaskFailRequest,
    TaskFinishRequest,
    TaskLogRequest,
    TaskRenewRequest,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# Query helpers
# ═══════════════════════════════════════════════════════════════════


async def get_task_by_id(db: AsyncSession, task_id: str) -> Task | None:
    result = await db.execute(select(Task).where(Task.task_id == task_id))
    return result.scalar_one_or_none()


async def get_all_tasks(db: AsyncSession, status_filter: str | None = None) -> list[Task]:
    query = select(Task).order_by(Task.created_at.desc())
    if status_filter:
        query = query.where(Task.status == status_filter)
    result = await db.execute(query)
    return list(result.scalars().all())


async def get_task_logs(db: AsyncSession, task_id: str) -> list[TaskLog]:
    result = await db.execute(
        select(TaskLog)
        .where(TaskLog.task_id == task_id)
        .order_by(TaskLog.log_time)
    )
    return list(result.scalars().all())


# ═══════════════════════════════════════════════════════════════════
# Task pull (with FOR UPDATE concurrency protection)
# ═══════════════════════════════════════════════════════════════════


async def pull_task_for_node(db: AsyncSession, node: ComputeNode) -> tuple[Task | None, int]:
    """Atomically pull a task for *node*.

    Returns (task, lease_duration) where lease_duration is the actual
    seconds assigned for this pull.

    Concurrency safety:
    - Locks the compute_nodes row with ``SELECT ... FOR UPDATE`` so
      concurrent pulls from the same node are serialised.
    - Inside the same transaction: re-checks running count from DB,
      picks a candidate, and conditionally UPDATEs the task.
    - If the UPDATE claims 0 rows (another node grabbed it), returns None.
    """
    # Lock the node row to serialise concurrent pulls for this node
    lock_stmt = select(ComputeNode).where(
        ComputeNode.node_id == node.node_id
    ).with_for_update()
    await db.execute(lock_stmt)

    # Re-check capacity inside the lock: count running tasks from DB
    active_result = await db.execute(
        select(Task).where(
            Task.assigned_node_id == node.node_id,
            Task.status == "running",
        )
    )
    db_running_count = len(list(active_result.scalars().all()))
    profile = node.static_profile or {}
    limits = profile.get("limits", {})
    max_parallel = limits.get("max_parallel_tasks", 1)

    if db_running_count >= max_parallel:
        logger.debug("Node %s at capacity (%d/%d) — skip pull",
                     node.node_id, db_running_count, max_parallel)
        await db.commit()  # release lock
        return (None, 0)

    # Pick best candidate
    candidate = await _pick_best_task(db, node, db_running_count, max_parallel)
    if candidate is None:
        await db.commit()
        return (None, 0)

    now = datetime.utcnow()
    lease_duration = min(candidate.timeout_seconds, settings.max_lease_seconds)
    lease_duration = max(lease_duration, settings.task_lease_seconds)
    lease_until = now + timedelta(seconds=lease_duration)

    stmt = (
        sql_update(Task)
        .where(
            Task.task_id == candidate.task_id,
            Task.status.in_(["pending", "retrying"]),
        )
        .values(
            status="running",
            assigned_node_id=node.node_id,
            lease_until=lease_until,
            started_at=now,
        )
    )
    result = await db.execute(stmt)
    await db.commit()  # commit releases the FOR UPDATE lock

    if result.rowcount == 0:
        logger.info("Task %s was already claimed", candidate.task_id)
        return (None, 0)

    return (await get_task_by_id(db, candidate.task_id), lease_duration)


# ═══════════════════════════════════════════════════════════════════
# Lease renewal
# ═══════════════════════════════════════════════════════════════════


async def renew_task_lease(
    db: AsyncSession,
    task_id: str,
    node_id: str,
    req: TaskRenewRequest,
) -> Task:
    """Extend the lease on a running task.

    lease_seconds is clamped to [task_lease_seconds, max_lease_seconds]
    to prevent clients from setting very short leases.
    """
    from fastapi import HTTPException

    task = await get_task_by_id(db, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status not in ("running", "assigned"):
        raise HTTPException(status_code=400, detail=f"Task status is {task.status}, cannot renew")
    if task.assigned_node_id != node_id:
        raise HTTPException(status_code=403, detail="Not the assigned node for this task")

    now = datetime.utcnow()
    safe_lease = max(settings.task_lease_seconds,
                     min(req.lease_seconds, settings.max_lease_seconds))
    new_lease = now + timedelta(seconds=safe_lease)
    task.lease_until = new_lease
    await db.commit()
    await db.refresh(task)
    return task


# ═══════════════════════════════════════════════════════════════════
# Log / finish / fail
# ═══════════════════════════════════════════════════════════════════


async def append_task_log(db: AsyncSession, task_id: str, node_id: str, req: TaskLogRequest):
    """Append a log entry.  Validates task assignment."""
    from fastapi import HTTPException

    result = await db.execute(
        select(Task).where(
            Task.task_id == task_id,
            Task.assigned_node_id == node_id,
        )
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=403, detail="Task not found or not assigned to this node")

    entry = TaskLog(
        task_id=task_id,
        node_id=node_id,
        level=req.level,
        message=req.message,
    )
    db.add(entry)
    await db.commit()


async def finish_task(db: AsyncSession, task_id: str, node_id: str, req: TaskFinishRequest):
    """Mark task as success.  Validates status & assignment."""
    result = await db.execute(
        select(Task).where(
            Task.task_id == task_id,
            Task.assigned_node_id == node_id,
            Task.status == "running",
        )
    )
    task = result.scalar_one_or_none()
    if not task:
        raise ValueError("Task not found, not assigned, or not running")

    now = datetime.utcnow()
    task.status = "success"
    task.result = req.result
    task.finished_at = now
    await db.commit()
    await db.refresh(task)
    return task


async def fail_task(db: AsyncSession, task_id: str, node_id: str, req: TaskFailRequest):
    """Report task failure.  Retries if max_retries not exceeded."""
    result = await db.execute(
        select(Task).where(
            Task.task_id == task_id,
            Task.assigned_node_id == node_id,
            Task.status == "running",
        )
    )
    task = result.scalar_one_or_none()
    if not task:
        raise ValueError("Task not found, not assigned, or not running")

    now = datetime.utcnow()
    if task.retry_count < task.max_retries:
        task.status = "retrying"
        task.retry_count += 1
        task.assigned_node_id = None
        task.lease_until = None
        task.started_at = None
        task.result = {
            "error": req.error,
            "traceback": req.traceback,
            "failed_node": node_id,
        }
    else:
        task.status = "failed"
        task.finished_at = now
        task.result = {
            "error": req.error,
            "traceback": req.traceback,
            "failed_node": node_id,
        }
    await db.commit()
    await db.refresh(task)
    return task


# ═══════════════════════════════════════════════════════════════════
# Scheduler helpers
# ═══════════════════════════════════════════════════════════════════


async def release_expired_leases(db: AsyncSession) -> list[Task]:
    """Reclaim tasks whose lease has expired."""
    now = datetime.utcnow()
    result = await db.execute(
        select(Task).where(
            Task.status.in_(["running", "assigned"]),
            Task.lease_until < now,
        )
    )
    expired = list(result.scalars().all())
    for task in expired:
        last_node = task.assigned_node_id

        if task.retry_count < task.max_retries:
            task.status = "retrying"
            task.retry_count += 1
            task.assigned_node_id = None
            task.lease_until = None
            task.started_at = None
            if task.result is None:
                task.result = {}
            task.result["last_error"] = "Lease expired (no renewal received)"
            task.result["last_node"] = last_node
        else:
            task.status = "timeout"
            task.finished_at = now
            if task.result is None:
                task.result = {}
            task.result["last_error"] = "Lease expired, max retries reached"
            task.result["last_node"] = last_node

    if expired:
        await db.commit()
    return expired


# ═══════════════════════════════════════════════════════════════════
# Scheduling logic
# ═══════════════════════════════════════════════════════════════════


async def _pick_best_task(
    db: AsyncSession,
    node: ComputeNode,
    db_running_count: int = 0,
    max_parallel: int = 1,
) -> Task | None:
    """Hard filter + scoring for task scheduling.

    *db_running_count* and *max_parallel* should be passed from the
    caller (which holds the FOR UPDATE lock) to avoid a TOCTOU race.
    """
    profile = node.static_profile or {}
    limits = profile.get("limits", {})

    result = await db.execute(
        select(Task)
        .where(Task.status.in_(["pending", "retrying"]))
        .order_by(Task.priority.desc(), Task.created_at.asc())
    )
    tasks = list(result.scalars().all())

    # Capacity check (using caller-provided values)
    if db_running_count >= max_parallel:
        logger.debug("Node %s at capacity (%d/%d)",
                     node.node_id, db_running_count, max_parallel)
        return None

    # Get node status for scoring
    ns_result = await db.execute(
        select(ComputeNodeStatus).where(ComputeNodeStatus.node_id == node.node_id)
    )
    ns = ns_result.scalar_one_or_none()

    candidates: list[Task] = []
    for task in tasks:
        reqs = task.requirements or {}
        payload = task.payload or {}
        execution = payload.get("execution", {})
        exec_mode = execution.get("mode", payload.get("mode", ""))

        # ── Hard filter: execution mode must be supported by node ──
        if exec_mode:
            node_runtime = profile.get("runtime", {})
            # Build the set of runtimes this node supports
            mode_to_runtime = {
                "shell": "shell",
                "python": "python",
                "docker": "docker",
                "hermes": "hermes",
            }
            runtime_key = mode_to_runtime.get(exec_mode)
            if runtime_key is None:
                # Unknown mode — skip (cannot determine capability)
                logger.debug("Task %s: unknown execution mode %r, skipping",
                             task.task_id, exec_mode)
                continue
            if not node_runtime.get(runtime_key, False):
                logger.debug("Task %s: node lacks runtime.%s for mode=%s, skipping",
                             task.task_id, runtime_key, exec_mode)
                continue

        # ── Tag filters ────────────────────────────────────────────
        required_tags = set(reqs.get("required_tags", []))
        node_tags = set(node.tags or [])
        if required_tags and not required_tags.issubset(node_tags):
            continue

        avoid_tags = set(reqs.get("avoid_tags", []))
        if avoid_tags & node_tags:
            continue

        task_runtime = reqs.get("runtime", {})
        node_runtime = profile.get("runtime", {})
        runtime_ok = all(
            not v or node_runtime.get(k, False)
            for k, v in task_runtime.items()
        )
        if not runtime_ok:
            continue

        if reqs.get("min_cpu_cores", 0) > profile.get("cpu_cores", 0):
            continue
        if reqs.get("min_memory_mb", 0) > profile.get("memory_mb", 0):
            continue
        if reqs.get("min_bandwidth_mbps", 0) > profile.get("bandwidth_mbps", 0):
            continue

        candidates.append(task)

    if not candidates:
        return None

    best_score = float("-inf")
    best_task = candidates[0]

    for task in candidates:
        score = 0.0
        reqs = task.requirements or {}

        idle_ratio = 1.0 - (db_running_count / max_parallel) if max_parallel > 0 else 1.0
        score += idle_ratio * 50

        if ns:
            score += (1.0 - ns.cpu_usage / 100.0) * 20
            score += (1.0 - ns.memory_usage / 100.0) * 20

        min_bw = reqs.get("min_bandwidth_mbps", 0)
        if min_bw > 0:
            node_bw = profile.get("bandwidth_mbps", 0)
            score += min(node_bw / 100.0, 1.0) * 10

        required_tags_set = set(reqs.get("required_tags", []))
        if "cn_reachable" in required_tags_set:
            cn_val = profile.get("cn_reachable", "poor")
            if cn_val == "good":
                score += 30
            elif cn_val == "fair":
                score += 10
        if "foreign_reachable" in required_tags_set:
            fg_val = profile.get("foreign_reachable", "poor")
            if fg_val == "excellent":
                score += 30
            elif fg_val == "good":
                score += 10

        if not limits.get("allow_heavy_compute", False):
            if reqs.get("min_cpu_cores", 0) > 2:
                score -= 100

        score += task.priority * 0.1

        if score > best_score:
            best_score = score
            best_task = task

    return best_task
