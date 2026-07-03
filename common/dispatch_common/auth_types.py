"""Auth type definitions that distinguish the two identity classes.

- Client identity  → user / session / client api token
- Compute identity → node_id + agent_token

These must NEVER be interchangeable.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TokenType(str, Enum):
    """Token classification — each maps to a specific auth pipeline."""

    # ── Client-side tokens ─────────────────────────────────────────
    CLIENT_API_TOKEN = "client_api_token"
    USER_SESSION = "user_session"

    # ── Compute-side tokens ────────────────────────────────────────
    COMPUTE_AGENT_TOKEN = "compute_agent_token"
    REGISTRATION_TOKEN = "registration_token"


class ClientAuthScope(str, Enum):
    """Scopes a client API token may carry."""

    TASK_CREATE = "task:create"
    TASK_READ = "task:read"
    TASK_CANCEL = "task:cancel"
    TASK_RETRY = "task:retry"
    TASK_LOG_READ = "task:log:read"
    TASK_ARTIFACT_READ = "task:artifact:read"
    NODE_READ = "node:read"
    NODE_MANAGE = "node:manage"
    USER_READ = "user:read"
    USER_MANAGE = "user:manage"
    AUDIT_READ = "audit:read"
    ADMIN = "admin"


@dataclass(frozen=True)
class ComputeNodeAuth:
    """Authenticated compute-server identity (NOT a client/user identity)."""

    node_id: str
    agent_token_hash: str

    def __repr__(self) -> str:
        return f"<ComputeNodeAuth node_id={self.node_id!r}>"
