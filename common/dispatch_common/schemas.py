"""Shared Pydantic schemas used between components.

These mirror the canonical definitions in the dispatcher's own schemas.py,
but live here so compute-server and client can validate without importing
dispatcher internals.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ── Node static profile (compact representation) ───────────────────


class NodeStaticProfile(BaseModel):
    """Shorthand: the subset of a compute server's profile that the
    dispatcher uses for scheduling decisions."""

    cpu_cores: int = 0
    memory_mb: int = 0
    bandwidth_mbps: int = 0
    public_ipv4: bool = False
    public_ipv6: bool = False
    cn_reachable: str = "poor"
    foreign_reachable: str = "poor"
    runtime: dict[str, bool] = {}
    limits: dict[str, Any] = {}


# ── Task payload ────────────────────────────────────────────────────


class TaskPayload(BaseModel):
    """Execution payload embedded in a task.

    The ``execution`` dict is what the compute-server's executor reads.
    """

    execution: dict[str, Any] = {}
    # Tasks may carry extra data alongside the execution spec
    extra: dict[str, Any] = {}


# ── Compute-server ↔ Dispatcher schemas ────────────────────────────


class ComputeRegisterRequest(BaseModel):
    """Sent by compute-server to /api/v1/compute/register."""

    node_id: str = Field(..., min_length=1, max_length=128)
    agent_token: str = Field(..., min_length=1)
    name: str = ""
    region: str = ""
    provider: str = ""
    roles: list[str] = []
    tags: list[str] = []
    static_profile: dict[str, Any] = {}


class ComputeHeartbeatRequest(BaseModel):
    """Sent by compute-server to /api/v1/compute/heartbeat."""

    cpu_usage: float = 0.0
    memory_usage: float = 0.0
    disk_usage: float = 0.0
    running_tasks: int = 0
    rx_mbps: float = 0.0
    tx_mbps: float = 0.0
    status_json: dict[str, Any] = {}


class ComputeNodeProfile(BaseModel):
    """Full node profile as seen by the dispatcher (for admin API responses)."""

    node_id: str
    name: str
    region: str
    provider: str
    enabled: bool
    roles: list[str]
    tags: list[str]
    static_profile: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class ComputeTaskPullResponse(BaseModel):
    """What the compute-server receives when it pulls a task."""

    task_id: str
    type: str
    payload: dict[str, Any]
    lease_until: datetime | None = None
    lease_seconds: int = 300  # actual lease duration for this pull
    timeout_seconds: int
    max_retries: int
    retry_count: int


# ── Client ↔ Dispatcher schemas ────────────────────────────────────


class ClientTaskCreateRequest(BaseModel):
    """Submitted by a client to /api/v1/client/tasks."""

    type: str = Field(..., min_length=1, max_length=128)
    priority: int = Field(default=50, ge=0, le=100)
    timeout_seconds: int = Field(default=3600, ge=1)
    max_retries: int = Field(default=3, ge=0)
    requirements: dict[str, Any] = {}
    payload: dict[str, Any] = {}


class ClientTaskResponse(BaseModel):
    """Task as returned to the client (no internal fields leaked)."""

    task_id: str
    type: str
    priority: int
    status: str
    requirements: dict[str, Any]
    payload: dict[str, Any]
    assigned_node_id: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    retry_count: int
    max_retries: int
    timeout_seconds: int
    result: dict[str, Any] | None = None

    model_config = {"from_attributes": True}
