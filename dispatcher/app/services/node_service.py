"""Node (compute-server) management service.

Registration, heartbeat, profile updates, and offline detection.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import ComputeNode, ComputeNodeStatus
from ..schemas import HeartbeatRequest, NodeRegisterRequest, NodeUpdateRequest

logger = logging.getLogger(__name__)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def register_node(db: AsyncSession, req: NodeRegisterRequest) -> ComputeNode:
    """Register or update a compute node.

    The agent_token is stored as a SHA-256 hash for security.
    """
    result = await db.execute(
        select(ComputeNode).where(ComputeNode.node_id == req.node_id)
    )
    node = result.scalar_one_or_none()
    now = datetime.utcnow()

    if node:
        node.name = req.name or node.name
        node.region = req.region or node.region
        node.provider = req.provider or node.provider
        node.roles = req.roles or node.roles
        node.tags = req.tags or node.tags
        node.static_profile = req.static_profile or node.static_profile
        if req.agent_token:
            node.agent_token_hash = _hash_token(req.agent_token)
        node.updated_at = now
    else:
        node = ComputeNode(
            node_id=req.node_id,
            name=req.name,
            region=req.region,
            provider=req.provider,
            roles=req.roles,
            tags=req.tags,
            static_profile=req.static_profile,
            agent_token_hash=_hash_token(req.agent_token) if req.agent_token else "",
        )
        db.add(node)
        await db.flush()

    # Ensure a status row exists
    result = await db.execute(
        select(ComputeNodeStatus).where(ComputeNodeStatus.node_id == req.node_id)
    )
    if not result.scalar_one_or_none():
        ns = ComputeNodeStatus(node_id=req.node_id, online=True, last_heartbeat=now)
        db.add(ns)

    await db.commit()
    await db.refresh(node)
    return node


async def update_node_profile(
    db: AsyncSession,
    node: ComputeNode,
    req: NodeUpdateRequest,
    actor_role: str | None = None,
) -> ComputeNode:
    """Update node static profile (does NOT change agent_token unless owner)."""
    if req.name is not None:
        node.name = req.name
    if req.region is not None:
        node.region = req.region
    if req.provider is not None:
        node.provider = req.provider
    if req.roles is not None:
        node.roles = req.roles
    if req.tags is not None:
        node.tags = req.tags
    if req.static_profile is not None:
        node.static_profile = req.static_profile
    if req.agent_token is not None:
        from ..auth import _check_role
        if actor_role and not _check_role("owner", actor_role):
            raise ValueError("Changing agent_token requires owner role")
        node.agent_token_hash = _hash_token(req.agent_token)
    node.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(node)
    return node


async def process_heartbeat(
    db: AsyncSession,
    node: ComputeNode,
    req: HeartbeatRequest,
) -> ComputeNodeStatus:
    """Update dynamic node status from heartbeat data."""
    now = datetime.utcnow()
    result = await db.execute(
        select(ComputeNodeStatus).where(ComputeNodeStatus.node_id == node.node_id)
    )
    ns = result.scalar_one_or_none()
    if not ns:
        ns = ComputeNodeStatus(node_id=node.node_id)
        db.add(ns)

    ns.online = True
    ns.last_heartbeat = now
    ns.cpu_usage = req.cpu_usage
    ns.memory_usage = req.memory_usage
    ns.disk_usage = req.disk_usage
    ns.running_tasks = req.running_tasks
    ns.rx_mbps = req.rx_mbps
    ns.tx_mbps = req.tx_mbps
    ns.status_json = req.status_json

    await db.commit()
    await db.refresh(ns)

    # Record metrics history (only when it's a full metrics report, not lightweight)
    status_json = req.status_json or {}
    if not status_json.get("lightweight"):
        from ..models import NodeMetricsHistory
        history = NodeMetricsHistory(
            node_id=node.node_id,
            recorded_at=now,
            cpu_usage=req.cpu_usage,
            memory_usage=req.memory_usage,
            disk_usage=req.disk_usage,
            rx_mbps=req.rx_mbps,
            tx_mbps=req.tx_mbps,
            running_tasks=req.running_tasks,
            status_json=req.status_json,
        )
        db.add(history)
        await db.commit()

    return ns


async def disable_node(db: AsyncSession, node_id: str):
    """Disable a node (stop task assignment)."""
    await db.execute(
        update(ComputeNode)
        .where(ComputeNode.node_id == node_id)
        .values(enabled=False)
    )
    await db.commit()


async def enable_node(db: AsyncSession, node_id: str):
    """Re-enable a node."""
    await db.execute(
        update(ComputeNode)
        .where(ComputeNode.node_id == node_id)
        .values(enabled=True)
    )
    await db.commit()


async def mark_node_offline(db: AsyncSession, node_id: str):
    """Mark a node as offline."""
    await db.execute(
        update(ComputeNodeStatus)
        .where(ComputeNodeStatus.node_id == node_id)
        .values(online=False)
    )
    await db.commit()


async def get_all_nodes(db: AsyncSession) -> list[ComputeNode]:
    result = await db.execute(select(ComputeNode).order_by(ComputeNode.node_id))
    return list(result.scalars().all())


async def get_node_by_id(db: AsyncSession, node_id: str) -> ComputeNode | None:
    result = await db.execute(
        select(ComputeNode).where(ComputeNode.node_id == node_id)
    )
    return result.scalar_one_or_none()


async def get_node_status(db: AsyncSession, node_id: str) -> ComputeNodeStatus | None:
    result = await db.execute(
        select(ComputeNodeStatus).where(ComputeNodeStatus.node_id == node_id)
    )
    return result.scalar_one_or_none()


async def detect_offline_nodes(db: AsyncSession) -> list[ComputeNodeStatus]:
    """Mark nodes as offline if they haven't heartbeaten within timeout."""
    threshold = datetime.utcnow() - timedelta(seconds=settings.node_offline_seconds)
    result = await db.execute(
        select(ComputeNodeStatus).where(
            ComputeNodeStatus.online == True,  # noqa: E712
            ComputeNodeStatus.last_heartbeat < threshold,
        )
    )
    stale = list(result.scalars().all())
    for ns in stale:
        ns.online = False
    if stale:
        await db.commit()
    return stale
