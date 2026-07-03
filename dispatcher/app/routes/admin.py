"""Admin API — /api/v1/admin/*"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import ClientAuthContext, hash_password, require_client_role
from ..database import get_db
from ..models import AuditLog, ComputeNode, ComputeNodeStatus, Task, User
from ..schemas import (
    AuditLogResponse,
    MessageResponse,
    NodeDetailResponse,
    NodeRegisterRequest,
    NodeResponse,
    NodeStatusResponse,
    NodeUpdateRequest,
    TaskResponse,
    UserCreateRequest,
    UserResponse,
    UserUpdateRequest,
)
from ..services.audit_service import get_audit_logs, log_audit
from ..services.node_service import (
    disable_node,
    enable_node,
    get_all_nodes,
    get_node_by_id,
    get_node_status,
    register_node,
    update_node_profile,
)

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


# ── Node management ──────────────────────────────────────────────────


@router.get("/nodes", response_model=list[NodeResponse])
async def admin_list_nodes(
    db: AsyncSession = Depends(get_db),
    ctx: ClientAuthContext = Depends(require_client_role("viewer")),
):
    """List all registered compute nodes."""
    nodes = await get_all_nodes(db)
    return [NodeResponse.model_validate(n) for n in nodes]


@router.get("/nodes/{node_id}", response_model=NodeDetailResponse)
async def admin_get_node(
    node_id: str,
    db: AsyncSession = Depends(get_db),
    ctx: ClientAuthContext = Depends(require_client_role("viewer")),
):
    """Get node detail including live status."""
    node = await get_node_by_id(db, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    status = await get_node_status(db, node_id)
    resp = NodeDetailResponse.model_validate(node)
    if status:
        resp.status = NodeStatusResponse.model_validate(status)
    return resp


@router.post("/nodes/register", response_model=MessageResponse)
async def admin_register_node(
    req: NodeRegisterRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx: ClientAuthContext = Depends(require_client_role("admin")),
):
    """Admin: register or update a compute node's profile.

    Does NOT require the registration_token — just admin+ client auth.

    Rules for agent_token:
    - If the node does NOT exist yet: admin may set the initial token.
    - If the node EXISTS and agent_token differs from stored hash:
      only owner may overwrite it (changing a running node's credential).
    - Profile-only updates (name/tags/region) are allowed for admin+.
    """
    from ..services.node_service import get_node_by_id
    existing = await get_node_by_id(db, req.node_id)

    if existing and req.agent_token:
        # Node exists and caller wants to change the token
        if not ctx.is_system and ctx.role != "owner":
            raise HTTPException(
                status_code=403,
                detail="Changing agent_token of an existing node requires owner role",
            )

    node = await register_node(db, req)
    await log_audit(
        db, "admin.node.register", user_id=str(ctx.user_id or "system"),
        target_type="compute_node", target_id=req.node_id,
        ip_address=request.client.host if request.client else None,
        detail={"node_id": req.node_id, "name": req.name, "is_update": existing is not None},
    )
    return MessageResponse(message=f"Node {req.node_id} registered/updated")


@router.patch("/nodes/{node_id}", response_model=NodeResponse)
async def admin_update_node(
    node_id: str,
    req: NodeUpdateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx: ClientAuthContext = Depends(require_client_role("admin")),
):
    """Update a node's profile. Changing agent_token requires owner."""
    node = await get_node_by_id(db, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")

    updated = await update_node_profile(db, node, req, actor_role=ctx.role)
    await log_audit(
        db, "admin.node.update", user_id=str(ctx.user_id or "system"),
        target_type="compute_node", target_id=node_id,
        ip_address=request.client.host if request.client else None,
        detail=req.model_dump(exclude_none=True),
    )
    return NodeResponse.model_validate(updated)


@router.post("/nodes/{node_id}/disable", response_model=MessageResponse)
async def admin_disable_node(
    node_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx: ClientAuthContext = Depends(require_client_role("admin")),
):
    """Disable a compute node — stops task assignment."""
    node = await get_node_by_id(db, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    await disable_node(db, node_id)
    await log_audit(
        db, "node.disable", user_id=str(ctx.user_id or "system"),
        target_type="compute_node", target_id=node_id,
        ip_address=request.client.host if request.client else None,
    )
    return MessageResponse(message=f"Node {node_id} disabled")


@router.post("/nodes/{node_id}/enable", response_model=MessageResponse)
async def admin_enable_node(
    node_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx: ClientAuthContext = Depends(require_client_role("admin")),
):
    """Re-enable a disabled compute node."""
    node = await get_node_by_id(db, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    await enable_node(db, node_id)
    await log_audit(
        db, "node.enable", user_id=str(ctx.user_id or "system"),
        target_type="compute_node", target_id=node_id,
        ip_address=request.client.host if request.client else None,
    )
    return MessageResponse(message=f"Node {node_id} enabled")


# ── Task oversight ───────────────────────────────────────────────────


@router.get("/tasks", response_model=list[TaskResponse])
async def admin_list_tasks(
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
    ctx: ClientAuthContext = Depends(require_client_role("admin")),
):
    """List all tasks (admin view — requires admin or owner role).

    viewer/operator should use /api/v1/client/tasks for scoped view.
    """
    from ..services.compute_task_service import get_all_tasks
    tasks = await get_all_tasks(db, status_filter=status)
    return [TaskResponse.model_validate(t) for t in tasks]


# ── Audit log ────────────────────────────────────────────────────────


@router.get("/audit-logs", response_model=list[AuditLogResponse])
async def admin_list_audit_logs(
    limit: int = 50,
    offset: int = 0,
    action: str | None = None,
    user_id: str | None = None,
    db: AsyncSession = Depends(get_db),
    ctx: ClientAuthContext = Depends(require_client_role("owner")),
):
    """View audit log entries (owner only)."""
    logs, total = await get_audit_logs(
        db, limit=limit, offset=offset, action=action, user_id=user_id,
    )
    return [AuditLogResponse.model_validate(l) for l in logs]


# ── User management ──────────────────────────────────────────────────


@router.get("/users", response_model=list[UserResponse])
async def admin_list_users(
    db: AsyncSession = Depends(get_db),
    ctx: ClientAuthContext = Depends(require_client_role("owner")),
):
    """List all users (owner only)."""
    result = await db.execute(select(User).order_by(User.username))
    users = list(result.scalars().all())
    return [UserResponse.model_validate(u) for u in users]


@router.post("/users", response_model=UserResponse)
async def admin_create_user(
    req: UserCreateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx: ClientAuthContext = Depends(require_client_role("owner")),
):
    """Create a new user (owner only)."""
    result = await db.execute(select(User).where(User.username == req.username))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Username already exists")

    user = User(
        username=req.username,
        password_hash=hash_password(req.password),
        role=req.role,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    await log_audit(
        db, "user.create", user_id=str(ctx.user_id or "system"),
        target_type="user", target_id=user.user_id,
        ip_address=request.client.host if request.client else None,
        detail={"username": req.username, "role": req.role},
    )
    return UserResponse.model_validate(user)


@router.patch("/users/{user_id}", response_model=UserResponse)
async def admin_update_user(
    user_id: str,
    req: UserUpdateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx: ClientAuthContext = Depends(require_client_role("owner")),
):
    """Update user (password, role, enabled). Owner only."""
    result = await db.execute(select(User).where(User.user_id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if req.password is not None:
        user.password_hash = hash_password(req.password)
    if req.role is not None:
        user.role = req.role
    if req.enabled is not None:
        user.enabled = req.enabled

    await db.commit()
    await db.refresh(user)

    changed = [k for k, v in req.model_dump(exclude_none=True).items()]
    await log_audit(
        db, "user.update", user_id=str(ctx.user_id or "system"),
        target_type="user", target_id=user_id,
        ip_address=request.client.host if request.client else None,
        detail={"changed_fields": changed},
    )
    return UserResponse.model_validate(user)
