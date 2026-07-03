"""Pydantic schemas for the dispatcher API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


# ═══════════════════════════════════════════════════════════════════
# Node (compute server) schemas
# ═══════════════════════════════════════════════════════════════════


class NodeRegisterRequest(BaseModel):
    node_id: str = Field(..., min_length=1, max_length=128)
    agent_token: str = Field(..., min_length=1)
    name: str = ""
    region: str = ""
    provider: str = ""
    roles: list[str] = []
    tags: list[str] = []
    static_profile: dict[str, Any] = {}


class NodeUpdateRequest(BaseModel):
    name: str | None = None
    region: str | None = None
    provider: str | None = None
    roles: list[str] | None = None
    tags: list[str] | None = None
    static_profile: dict[str, Any] | None = None
    agent_token: str | None = None  # changing requires owner role


class HeartbeatRequest(BaseModel):
    cpu_usage: float = 0.0
    memory_usage: float = 0.0
    disk_usage: float = 0.0
    running_tasks: int = 0
    rx_mbps: float = 0.0
    tx_mbps: float = 0.0
    status_json: dict[str, Any] = {}


class NodeResponse(BaseModel):
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

    model_config = {"from_attributes": True}


class NodeDetailResponse(NodeResponse):
    status: Optional["NodeStatusResponse"] = None


class NodeStatusResponse(BaseModel):
    online: bool
    last_heartbeat: Optional[datetime] = None
    cpu_usage: float
    memory_usage: float
    disk_usage: float
    running_tasks: int
    rx_mbps: float
    tx_mbps: float
    status_json: dict[str, Any]

    model_config = {"from_attributes": True}


# ═══════════════════════════════════════════════════════════════════
# Task schemas
# ═══════════════════════════════════════════════════════════════════


class TaskCreateRequest(BaseModel):
    type: str = Field(..., min_length=1, max_length=128)
    priority: int = Field(default=50, ge=0, le=100)
    timeout_seconds: int = Field(default=3600, ge=1)
    max_retries: int = Field(default=3, ge=0)
    requirements: dict[str, Any] = {}
    payload: dict[str, Any] = {}


class TaskRenewRequest(BaseModel):
    lease_seconds: int = Field(default=300, ge=30, le=3600)


class TaskLogRequest(BaseModel):
    level: str = "INFO"
    message: str


class TaskFinishRequest(BaseModel):
    result: dict[str, Any] = {}


class TaskFailRequest(BaseModel):
    error: str = ""
    traceback: str = ""


class TaskPullResponse(BaseModel):
    """What the compute server receives on pull."""
    task_id: str
    type: str
    payload: dict[str, Any]
    lease_until: Optional[datetime] = None
    lease_seconds: int = 300  # actual lease duration for this pull
    timeout_seconds: int
    max_retries: int
    retry_count: int


class TaskResponse(BaseModel):
    task_id: str
    type: str
    priority: int
    status: str
    requirements: dict[str, Any]
    payload: dict[str, Any]
    assigned_node_id: Optional[str] = None
    lease_until: Optional[datetime] = None
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    retry_count: int
    max_retries: int
    timeout_seconds: int
    result: Optional[dict[str, Any]] = None

    model_config = {"from_attributes": True}


# ═══════════════════════════════════════════════════════════════════
# Auth / User schemas
# ═══════════════════════════════════════════════════════════════════


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=256)


class LoginResponse(BaseModel):
    message: str
    user_id: str
    username: str
    role: str


class UserCreateRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=64, pattern=r"^[a-zA-Z0-9_\-]+$")
    password: str = Field(..., min_length=8, max_length=256)
    role: str = Field(default="viewer", pattern=r"^(viewer|operator|admin|owner)$")


class UserUpdateRequest(BaseModel):
    password: str | None = None
    role: str | None = Field(default=None, pattern=r"^(viewer|operator|admin|owner)$")
    enabled: bool | None = None


class UserResponse(BaseModel):
    user_id: str
    username: str
    role: str
    enabled: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class MeResponse(BaseModel):
    user_id: str
    username: str
    role: str


# ═══════════════════════════════════════════════════════════════════
# Audit / Utility
# ═══════════════════════════════════════════════════════════════════


class AuditLogResponse(BaseModel):
    id: int
    user_id: Optional[str] = None
    action: str
    target_type: Optional[str] = None
    target_id: Optional[str] = None
    ip_address: Optional[str] = None
    detail: Optional[dict[str, Any]] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class MessageResponse(BaseModel):
    message: str
