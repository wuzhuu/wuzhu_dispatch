"""Auth endpoints for client identity management.

  POST /api/v1/auth/login   — Login (set session cookie)
  POST /api/v1/auth/logout  — Logout (clear session)
  GET  /api/v1/auth/me      — Current user info
"""

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import (
    ClientAuthContext,
    authenticate_client,
    generate_session_id,
    get_current_user_from_session,
    hash_password,
    require_role,
    verify_password,
)
from ..config import settings
from ..database import get_db
from ..middleware.security import csrf_token_hmac
from ..models import Session, User
from ..schemas import (
    LoginRequest,
    LoginResponse,
    MeResponse,
    MessageResponse,
)
from ..services.audit_service import log_audit

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
async def login(
    req: LoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Authenticate and create a session cookie."""
    result = await db.execute(
        select(User).where(User.username == req.username, User.enabled == True)  # noqa: E712
    )
    user = result.scalar_one_or_none()

    if not user or not verify_password(req.password, user.password_hash):
        await log_audit(
            db, "login.failed", user_id=req.username,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("User-Agent", ""),
        )
        raise HTTPException(status_code=401, detail="Invalid username or password")

    session_id = generate_session_id()
    expires_at = datetime.utcnow() + timedelta(seconds=settings.session_ttl_seconds)
    session = Session(
        session_id=session_id,
        user_id=user.user_id,
        expires_at=expires_at,
    )
    db.add(session)
    await db.commit()

    response.set_cookie(
        key="dispatch_session",
        value=session_id,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=False,
        samesite="lax",
        path="/",
    )
    # CSRF token cookie (JS-readable)
    csrf_value = csrf_token_hmac(session_id, settings.session_secret)
    response.set_cookie(
        key="csrf_token",
        value=csrf_value,
        max_age=settings.session_ttl_seconds,
        httponly=False,
        secure=False,
        samesite="lax",
        path="/",
    )

    await log_audit(
        db, "login.success", user_id=user.user_id,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("User-Agent", ""),
    )

    return LoginResponse(
        message="Login successful",
        user_id=user.user_id,
        username=user.username,
        role=user.role,
    )


@router.post("/logout", response_model=MessageResponse)
async def logout(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Destroy the current session."""
    session_id = request.cookies.get("dispatch_session")
    if session_id:
        result = await db.execute(
            select(Session).where(Session.session_id == session_id)
        )
        sess = result.scalar_one_or_none()
        if sess:
            await log_audit(
                db, "logout", user_id=sess.user_id,
                ip_address=request.client.host if request.client else None,
            )
            await db.delete(sess)
            await db.commit()

    response.delete_cookie("dispatch_session", path="/")
    response.delete_cookie("csrf_token", path="/")
    return MessageResponse(message="Logged out")


@router.get("/me", response_model=MeResponse)
async def get_me(
    ctx: ClientAuthContext = Depends(authenticate_client),
):
    """Return the current user info."""
    return MeResponse(
        user_id=ctx.user_id,
        username=ctx.user.username if ctx.user else "system",
        role=ctx.role,
    )
