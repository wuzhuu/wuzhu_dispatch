"""SQLAlchemy ORM models for wuzhu-dispatch.

Tables:
  users               — Client-side user accounts (RBAC)
  sessions            — Web session cookies
  client_api_tokens   — Long-lived client API tokens (separate from node tokens)
  compute_nodes       — Compute server static registration & profile
  compute_node_status — Compute server dynamic heartbeat state
  tasks               — Task queue
  task_logs           — Per-task log entries
  artifacts           — Task output references
  audit_logs          — Audit trail
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, Float, ForeignKey,
    Integer, String, Text,
)
from sqlalchemy import JSON as JSONType
from sqlalchemy.orm import Mapped

from .database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


# ═══════════════════════════════════════════════════════════════════
# Client-side identity
# ═══════════════════════════════════════════════════════════════════


class User(Base):
    """Human or service account with RBAC role.

    This is a CLIENT identity — it has NO access to compute-server endpoints.
    """
    __tablename__ = "users"

    user_id: Mapped[str] = Column(String(64), primary_key=True, default=_uuid)
    username: Mapped[str] = Column(String(64), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = Column(String(255), nullable=False)
    role: Mapped[str] = Column(String(32), nullable=False, default="viewer")
    #  viewer / operator / admin / owner
    enabled: Mapped[bool] = Column(Boolean, default=True)
    created_at: Mapped[datetime] = Column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<User {self.username!r} role={self.role!r}>"


class ClientApiToken(Base):
    """Long-lived token for programmatic client access.

    This is NOT a compute-server token.  It can only be used against
    /api/v1/client/* and /api/v1/admin/* endpoints.
    """
    __tablename__ = "client_api_tokens"

    token_id: Mapped[str] = Column(String(64), primary_key=True, default=_uuid)
    token_hash: Mapped[str] = Column(String(255), nullable=False)
    user_id: Mapped[str] = Column(String(64), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False, index=True)
    scope: Mapped[list] = Column(JSONType, default=list)  # e.g. ["task:create", "task:read"]
    enabled: Mapped[bool] = Column(Boolean, default=True)
    created_at: Mapped[datetime] = Column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime | None] = Column(DateTime, nullable=True)
    last_used_at: Mapped[datetime | None] = Column(DateTime, nullable=True)

    def __repr__(self) -> str:
        return f"<ClientApiToken {self.token_id[:12]}... user={self.user_id!r}>"


class Session(Base):
    """Web admin session (HttpOnly cookie)."""
    __tablename__ = "sessions"

    session_id: Mapped[str] = Column(String(128), primary_key=True, default=_uuid)
    user_id: Mapped[str] = Column(String(64), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False, index=True)
    expires_at: Mapped[datetime] = Column(DateTime, nullable=False)
    created_at: Mapped[datetime] = Column(DateTime, default=datetime.utcnow)
    last_seen_at: Mapped[datetime | None] = Column(DateTime, nullable=True)

    def __repr__(self) -> str:
        return f"<Session user_id={self.user_id!r}>"


# ═══════════════════════════════════════════════════════════════════
# Compute-server identity (separate from client identity)
# ═══════════════════════════════════════════════════════════════════


class ComputeNode(Base):
    """Static registration / profile of a compute server node.

    Only the dispatcher reads/writes this table.
    Compute servers authenticate via node_id + agent_token (stored hashed).
    """
    __tablename__ = "compute_nodes"

    id: Mapped[int] = Column(Integer, primary_key=True, autoincrement=True)
    node_id: Mapped[str] = Column(String(128), unique=True, nullable=False, index=True)
    name: Mapped[str] = Column(String(256), default="")
    region: Mapped[str] = Column(String(64), default="")
    provider: Mapped[str] = Column(String(128), default="")
    enabled: Mapped[bool] = Column(Boolean, default=True)
    roles: Mapped[list] = Column(JSONType, default=list)
    tags: Mapped[list] = Column(JSONType, default=list)
    static_profile: Mapped[dict] = Column(JSONType, default=dict)

    # agent_token is stored as a hash (SHA-256 of the raw token)
    agent_token_hash: Mapped[str] = Column(String(64), default="")

    created_at: Mapped[datetime] = Column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<ComputeNode node_id={self.node_id!r}>"


class ComputeNodeStatus(Base):
    """Dynamic heartbeat / runtime state of a compute server."""
    __tablename__ = "compute_node_status"

    id: Mapped[int] = Column(Integer, primary_key=True, autoincrement=True)
    node_id: Mapped[str] = Column(
        String(128),
        ForeignKey("compute_nodes.node_id", ondelete="CASCADE"),
        unique=True, nullable=False, index=True,
    )
    online: Mapped[bool] = Column(Boolean, default=False)
    last_heartbeat: Mapped[datetime | None] = Column(DateTime, nullable=True)
    cpu_usage: Mapped[float] = Column(Float, default=0.0)
    memory_usage: Mapped[float] = Column(Float, default=0.0)
    disk_usage: Mapped[float] = Column(Float, default=0.0)
    running_tasks: Mapped[int] = Column(Integer, default=0)
    rx_mbps: Mapped[float] = Column(Float, default=0.0)
    tx_mbps: Mapped[float] = Column(Float, default=0.0)
    status_json: Mapped[dict] = Column(JSONType, default=dict)

    def __repr__(self) -> str:
        return f"<ComputeNodeStatus node_id={self.node_id!r} online={self.online}>"


# ═══════════════════════════════════════════════════════════════════
# Tasks
# ═══════════════════════════════════════════════════════════════════


class Task(Base):
    """Work unit — created by a client, dispatched to a compute server."""
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = Column(String(128), unique=True, nullable=False, index=True, default=_uuid)

    # ── Ownership ──────────────────────────────────────────────────
    created_by_user_id: Mapped[str | None] = Column(String(64), ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True, index=True)
    created_by_client_token_id: Mapped[str | None] = Column(String(64), nullable=True)

    # ── Task spec ──────────────────────────────────────────────────
    type: Mapped[str] = Column(String(128), nullable=False, index=True)
    priority: Mapped[int] = Column(Integer, default=50)
    status: Mapped[str] = Column(String(32), default="pending", index=True)
    requirements: Mapped[dict] = Column(JSONType, default=dict)
    payload: Mapped[dict] = Column(JSONType, default=dict)

    # ── Scheduling ─────────────────────────────────────────────────
    assigned_node_id: Mapped[str | None] = Column(String(128), nullable=True, index=True)
    lease_until: Mapped[datetime | None] = Column(DateTime, nullable=True)
    timeout_seconds: Mapped[int] = Column(Integer, default=3600)
    max_retries: Mapped[int] = Column(Integer, default=3)
    retry_count: Mapped[int] = Column(Integer, default=0)

    # ── Result / timestamps ────────────────────────────────────────
    result: Mapped[dict | None] = Column(JSONType, nullable=True)
    created_at: Mapped[datetime] = Column(DateTime, default=datetime.utcnow)
    started_at: Mapped[datetime | None] = Column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = Column(DateTime, nullable=True)

    def __repr__(self) -> str:
        return f"<Task {self.task_id[:12]}... status={self.status!r}>"


class TaskLog(Base):
    """Per-task log entries streamed from the compute server."""
    __tablename__ = "task_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = Column(
        String(128), ForeignKey("tasks.task_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    node_id: Mapped[str | None] = Column(
        String(128), ForeignKey("compute_nodes.node_id", ondelete="SET NULL"),
        nullable=True,
    )
    log_time: Mapped[datetime] = Column(DateTime, default=datetime.utcnow)
    level: Mapped[str] = Column(String(16), default="INFO")
    message: Mapped[str] = Column(Text, default="")

    def __repr__(self) -> str:
        return f"<TaskLog task_id={self.task_id[:12]}... level={self.level!r}>"


class Artifact(Base):
    """Reference to output files produced by a task."""
    __tablename__ = "artifacts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    artifact_id: Mapped[str] = Column(String(128), unique=True, nullable=False, default=_uuid, index=True)
    task_id: Mapped[str] = Column(
        String(128), ForeignKey("tasks.task_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    node_id: Mapped[str | None] = Column(
        String(128), ForeignKey("compute_nodes.node_id", ondelete="SET NULL"),
        nullable=True,
    )
    file_name: Mapped[str] = Column(String(256), default="")
    storage_type: Mapped[str] = Column(String(32), default="local")
    storage_path: Mapped[str] = Column(String(1024), default="")
    file_size: Mapped[int] = Column(BigInteger, default=0)
    sha256: Mapped[str] = Column(String(64), default="")
    created_at: Mapped[datetime] = Column(DateTime, default=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<Artifact {self.artifact_id[:12]}...>"


class AuditLog(Base):
    """Audit trail for sensitive operations."""
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str | None] = Column(String(64), nullable=True)
    action: Mapped[str] = Column(String(128), nullable=False, index=True)
    target_type: Mapped[str | None] = Column(String(64), nullable=True)
    target_id: Mapped[str | None] = Column(String(128), nullable=True)
    ip_address: Mapped[str | None] = Column(String(64), nullable=True)
    user_agent: Mapped[str | None] = Column(Text, nullable=True)
    detail: Mapped[dict | None] = Column(JSONType, nullable=True)
    created_at: Mapped[datetime] = Column(DateTime, default=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<AuditLog action={self.action!r}>"
