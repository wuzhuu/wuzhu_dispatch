"""Authentication module: dual identity system.

Two completely separate auth pipelines:

  1. Client identity  → users / sessions / client_api_tokens
  2. Compute identity → compute_nodes (node_id + agent_token)

These share no code paths and return different FastAPI dependency types
so they can never be confused.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timedelta
from typing import Literal

import bcrypt
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .database import get_db
from .models import ClientApiToken, ComputeNode, Session, User

logger = logging.getLogger(__name__)

ROLE_HIERARCHY = ["viewer", "operator", "admin", "owner"]


# ═══════════════════════════════════════════════════════════════════
# Shared helpers
# ═══════════════════════════════════════════════════════════════════


def _check_role(required: str, actual: str) -> bool:
    """True if *actual* role has at least *required* privilege."""
    try:
        return ROLE_HIERARCHY.index(actual) >= ROLE_HIERARCHY.index(required)
    except (ValueError, IndexError):
        return False


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=settings.bcrypt_rounds)).decode()


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())


def generate_session_id() -> str:
    return secrets.token_urlsafe(48)


def sha256_hash(value: str) -> str:
    """Deterministic SHA-256 hex digest (for agent token hashing)."""
    return hashlib.sha256(value.encode()).hexdigest()


# ═══════════════════════════════════════════════════════════════════
# CLIENT identity: user / session / client API token
# ═══════════════════════════════════════════════════════════════════


async def get_current_user_from_session(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User | None:
    """Extract the user from an HttpOnly session cookie."""
    session_id = request.cookies.get("dispatch_session")
    if not session_id:
        return None

    result = await db.execute(
        select(Session).where(Session.session_id == session_id)
    )
    sess = result.scalar_one_or_none()
    if not sess:
        return None
    if sess.expires_at < datetime.utcnow():
        await db.delete(sess)
        await db.commit()
        return None

    sess.last_seen_at = datetime.utcnow()
    await db.commit()

    result = await db.execute(select(User).where(User.user_id == sess.user_id))
    user = result.scalar_one_or_none()
    if not user or not user.enabled:
        return None
    return user


async def require_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User:
    """Require a valid logged-in user (session cookie)."""
    user = await get_current_user_from_session(request, db)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return user


def require_role(min_role: str):
    """Factory: dependency that checks the user has at least *min_role*."""
    async def _checker(current_user: User = Depends(require_user)) -> User:
        if not _check_role(min_role, current_user.role):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires role '{min_role}' or higher, "
                       f"current role is '{current_user.role}'",
            )
        return current_user
    return _checker


class ClientAuthContext:
    """Result of client authentication — either a User (session) or
    system-level (admin Bearer / valid client API token)."""

    def __init__(self, user: User | None = None, is_system: bool = False,
                 token_id: str | None = None, token_scope: list | None = None):
        self.user = user
        self.is_system = is_system
        self.token_id = token_id
        self.token_scope = token_scope or []

    @property
    def user_id(self) -> str | None:
        if self.user:
            return self.user.user_id
        return None  # system tokens have no user_id — FK-safe

    @property
    def role(self) -> str:
        if self.user:
            return self.user.role
        return "owner"  # system-level tokens get full access


async def authenticate_client(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> ClientAuthContext:
    """Authenticate a client request.

    Priority:
      1. DISPATCH_SERVER_SECRET Bearer token (system-level bootstrap)
      2. Client API token (Bearer)
      3. Session cookie
    """
    auth = request.headers.get("Authorization", "")

    # ── Bearer token path ──────────────────────────────────────────
    if auth.startswith("Bearer "):
        token = auth[7:]

        # 1a. Server admin secret (system-level bootstrap)
        if hmac.compare_digest(token, settings.dispatch_server_secret):
            return ClientAuthContext(is_system=True)

        # 1b. Client API token
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        result = await db.execute(
            select(ClientApiToken).where(
                ClientApiToken.token_hash == token_hash,
                ClientApiToken.enabled == True,  # noqa: E712
            )
        )
        api_token = result.scalar_one_or_none()
        if api_token:
            if api_token.expires_at and api_token.expires_at < datetime.utcnow():
                raise HTTPException(status_code=403, detail="API token expired")
            api_token.last_used_at = datetime.utcnow()
            await db.commit()
            # Return the owning user
            user_result = await db.execute(
                select(User).where(User.user_id == api_token.user_id)
            )
            user = user_result.scalar_one_or_none()
            if not user or not user.enabled:
                raise HTTPException(status_code=403, detail="User disabled")
            return ClientAuthContext(
                user=user, token_id=api_token.token_id,
                token_scope=api_token.scope or [],
            )

    # ── Session cookie path ────────────────────────────────────────
    user = await get_current_user_from_session(request, db)
    if user:
        return ClientAuthContext(user=user)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated (provide Bearer token or login)",
    )


def require_client_role(min_role: str):
    """Factory: dependency for client-facing endpoints.

    Ensures the caller has at least *min_role* via session or token.
    """
    async def _checker(
        ctx: ClientAuthContext = Depends(authenticate_client),
    ) -> ClientAuthContext:
        if ctx.is_system:
            return ctx  # admin token = full access
        if not _check_role(min_role, ctx.role):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires role '{min_role}' or higher",
            )
        return ctx
    return _checker


# ═══════════════════════════════════════════════════════════════════
# Client API Token scope / capability validation
# ═══════════════════════════════════════════════════════════════════


def get_token_capabilities(scope: list | None) -> dict:
    """Extract structured capabilities from ``scope``.

    The scope can be a simple list like ``["task:create"]`` or a dict
    embedded in the list for JSON column compatibility::

        [{"allowed_templates": ["http_probe"],
          "max_priority": 50, "max_timeout_seconds": 300}]

    Returns a dict with safe defaults.
    """
    caps: dict = {}
    if scope:
        for item in scope:
            if isinstance(item, dict):
                caps.update(item)
    return {
        "allowed_templates": caps.get("allowed_templates", []),
        "allowed_modes": caps.get("allowed_modes", []),
        "denied_modes": caps.get("denied_modes", []),
        "allowed_target_tags": caps.get("allowed_target_tags", []),
        "allowed_node_ids": caps.get("allowed_node_ids", []),
        "max_priority": caps.get("max_priority", 100),
        "max_timeout_seconds": caps.get("max_timeout_seconds", 3600),
        "max_concurrent_tasks": caps.get("max_concurrent_tasks", 10),
        "max_payload_bytes": caps.get("max_payload_bytes", 65536),
        "can_target_specific_node": caps.get("can_target_specific_node", False),
        "allow_internal_network": caps.get("allow_internal_network", False),
    }


def validate_template_allowed(template_id: str, caps: dict):
    """Raise ``HTTPException(403)`` if template not allowed."""
    if caps["allowed_templates"] and template_id not in caps["allowed_templates"]:
        raise HTTPException(
            status_code=403,
            detail=f"Template {template_id!r} not in token's allowed_templates",
        )


def validate_target_tags(target_tags: list[str], caps: dict):
    """Raise ``HTTPException(403)`` if any tag outside allowed."""
    allowed = caps.get("allowed_target_tags", [])
    if allowed:
        for tag in target_tags:
            if tag not in allowed:
                raise HTTPException(
                    status_code=403,
                    detail=f"Tag {tag!r} not in token's allowed_target_tags",
                )


def validate_can_target_node(ctx: ClientAuthContext, caps: dict):
    """Raise ``HTTPException(403)`` if token can't target specific nodes."""
    if not ctx.is_system and ctx.role not in ("admin", "owner"):
        if not caps.get("can_target_specific_node", False):
            raise HTTPException(
                status_code=403,
                detail="Token does not allow targeting specific nodes",
            )


def validate_task_timeout(timeout_seconds: int, caps: dict):
    """Raise ``HTTPException(403)`` if timeout exceeds token limit."""
    max_timeout = caps.get("max_timeout_seconds", 3600)
    if timeout_seconds > max_timeout:
        raise HTTPException(
            status_code=403,
            detail=f"timeout_seconds {timeout_seconds} exceeds token limit {max_timeout}",
        )


def validate_task_priority(priority: int, caps: dict):
    """Raise ``HTTPException(403)`` if priority exceeds token limit."""
    max_prio = caps.get("max_priority", 100)
    if priority > max_prio:
        raise HTTPException(
            status_code=403,
            detail=f"priority {priority} exceeds token limit {max_prio}",
        )


def validate_mode_allowed(mode: str, caps: dict):
    """Validate *mode* against ``allowed_modes`` / ``denied_modes``.

    - Template tasks use mode="template".
    - Direct tasks use the execution mode (shell, hermes, etc.).
    - If ``denied_modes`` contains the mode → 403.
    - If ``allowed_modes`` is non-empty and does NOT contain the mode → 403.
    - If both lists are empty → allow (legacy / full-access token / no capabilities set).
    """
    denied = caps.get("denied_modes", [])
    if not denied and not caps.get("allowed_modes", []):
        return  # No capability constraints — allow anything
    if mode in denied:
        raise HTTPException(
            status_code=403,
            detail=f"Mode {mode!r} is denied by token's denied_modes",
        )
    allowed = caps.get("allowed_modes", [])
    if allowed and mode not in allowed:
        raise HTTPException(
            status_code=403,
            detail=f"Mode {mode!r} is not in token's allowed_modes {allowed}",
        )


# ═══════════════════════════════════════════════════════════════════
# COMPUTE identity: node_id + agent_token
# ═══════════════════════════════════════════════════════════════════


async def verify_compute_node(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> ComputeNode:
    """Validate compute-server authentication.

    Headers:
      Authorization: Bearer <agent_token>
      X-Node-Id: <node_id>

    Returns the authenticated ComputeNode object.
    This can ONLY be used by compute-server endpoints.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    bearer_token = auth[7:]

    node_id = request.headers.get("X-Node-Id", "")
    if not node_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing X-Node-Id header",
        )

    result = await db.execute(
        select(ComputeNode).where(ComputeNode.node_id == node_id)
    )
    node = result.scalar_one_or_none()
    if not node:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Compute node {node_id!r} not found",
        )
    if not node.enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Compute node {node_id!r} is disabled",
        )

    # Compare token against stored hash
    token_hash = sha256_hash(bearer_token)
    if not hmac.compare_digest(node.agent_token_hash, token_hash):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid agent token",
        )
    return node


async def verify_registration_token(request: Request):
    """Require registration_token for compute-server registration."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = auth[7:]
    expected = settings.effective_registration_token
    if not hmac.compare_digest(token, expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid registration token",
        )
    return True
