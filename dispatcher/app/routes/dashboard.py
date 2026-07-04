"""Dashboard API — /api/v1/admin/dashboard/*

Endpoints for the Web Dashboard.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import ClientAuthContext, require_client_role
from ..database import get_db
from ..models import ComputeNode, ComputeNodeStatus, NodeMetricsHistory, Task, TaskLog

router = APIRouter(prefix="/api/v1/admin/dashboard", tags=["dashboard"])


@router.get("/summary")
async def dashboard_summary(
    db: AsyncSession = Depends(get_db),
    ctx: ClientAuthContext = Depends(require_client_role("admin")),
):
    """Aggregated summary for the dashboard overview page."""
    # Node counts
    node_result = await db.execute(select(ComputeNode))
    all_nodes = list(node_result.scalars().all())
    total_nodes = len(all_nodes)

    ns_result = await db.execute(
        select(ComputeNodeStatus).where(ComputeNodeStatus.online == True)  # noqa: E712
    )
    online_nodes = len(list(ns_result.scalars().all()))

    # Task counts
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    pending_count = await _count_tasks_by_status(db, "pending")
    running_count = await _count_tasks_by_status(db, "running")
    success_today = await _count_tasks_since(db, "success", today)
    failed_today = await _count_tasks_since(db, "failed", today)

    # Aggregate resources (average across online nodes)
    avg_cpu = 0.0
    avg_mem = 0.0
    avg_disk = 0.0
    total_rx = 0.0
    total_tx = 0.0
    if online_nodes > 0:
        ns_all = await db.execute(select(ComputeNodeStatus).where(
            ComputeNodeStatus.online == True  # noqa: E712
        ))
        for ns in ns_all.scalars().all():
            avg_cpu += ns.cpu_usage
            avg_mem += ns.memory_usage
            avg_disk += ns.disk_usage
            total_rx += ns.rx_mbps
            total_tx += ns.tx_mbps
        avg_cpu /= online_nodes
        avg_mem /= online_nodes
        avg_disk /= online_nodes

    return {
        "nodes": {
            "total": total_nodes,
            "online": online_nodes,
            "offline": total_nodes - online_nodes,
        },
        "tasks": {
            "pending": pending_count,
            "running": running_count,
            "success_today": success_today,
            "failed_today": failed_today,
        },
        "resources": {
            "avg_cpu_usage": round(avg_cpu, 1),
            "avg_memory_usage": round(avg_mem, 1),
            "avg_disk_usage": round(avg_disk, 1),
            "total_rx_mbps": round(total_rx, 1),
            "total_tx_mbps": round(total_tx, 1),
        },
    }


@router.get("/nodes")
async def dashboard_nodes(
    db: AsyncSession = Depends(get_db),
    ctx: ClientAuthContext = Depends(require_client_role("admin")),
):
    """Nodes list with live status for dashboard."""
    result = await db.execute(select(ComputeNode).order_by(ComputeNode.node_id))
    nodes = []
    for node in result.scalars().all():
        ns_result = await db.execute(
            select(ComputeNodeStatus).where(ComputeNodeStatus.node_id == node.node_id)
        )
        ns = ns_result.scalar_one_or_none()
        hardware = (ns.status_json or {}).get("hardware", {}) if ns else {}
        nodes.append({
            "node_id": node.node_id,
            "name": node.name,
            "region": node.region,
            "provider": node.provider,
            "tags": node.tags,
            "enabled": node.enabled,
            "online": ns.online if ns else False,
            "last_heartbeat": ns.last_heartbeat.isoformat() if ns and ns.last_heartbeat else None,
            "cpu_usage": ns.cpu_usage if ns else 0,
            "memory_usage": ns.memory_usage if ns else 0,
            "disk_usage": ns.disk_usage if ns else 0,
            "total_cpu_cores": hardware.get("cpu_cores", 0),
            "total_memory_mb": hardware.get("memory_mb", 0),
            "total_disk_mb": hardware.get("disk_mb", 0),
            "running_tasks": ns.running_tasks if ns else 0,
            "rx_mbps": ns.rx_mbps if ns else 0,
            "tx_mbps": ns.tx_mbps if ns else 0,
            "max_parallel_tasks": node.static_profile.get("limits", {}).get("max_parallel_tasks", 1),
            "runtime": node.static_profile.get("runtime", {}),
        })
    return nodes


@router.get("/recent-tasks")
async def dashboard_recent_tasks(
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
    ctx: ClientAuthContext = Depends(require_client_role("admin")),
):
    """Recent tasks for the dashboard."""
    result = await db.execute(
        select(Task).order_by(Task.created_at.desc()).limit(limit)
    )
    tasks = []
    for t in result.scalars().all():
        tasks.append({
            "task_id": t.task_id,
            "type": t.type,
            "priority": t.priority,
            "status": t.status,
            "assigned_node_id": t.assigned_node_id,
            "retry_count": t.retry_count,
            "max_retries": t.max_retries,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "started_at": t.started_at.isoformat() if t.started_at else None,
            "finished_at": t.finished_at.isoformat() if t.finished_at else None,
        })
    return tasks


# ── Helpers ──────────────────────────────────────────────────────────


async def _count_tasks_by_status(db: AsyncSession, status: str) -> int:
    result = await db.execute(
        select(func.count(Task.id)).where(Task.status == status)
    )
    return result.scalar() or 0


async def _count_tasks_since(db: AsyncSession, status: str, since: datetime) -> int:
    result = await db.execute(
        select(func.count(Task.id)).where(
            Task.status == status,
            Task.finished_at >= since,
        )
    )
    return result.scalar() or 0
