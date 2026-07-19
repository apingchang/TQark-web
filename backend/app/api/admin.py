"""
Admin routes

- POST /admin/requests/{id}/approve  → 通過 user 申請
- POST /admin/requests/{id}/reject   → 拒絕
- POST /admin/users/{id}/ban         → ban user
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db_helpers import log_action
from app.core.deps import require_admin
from app.db.models import AccessRequest, User
from app.db.session import get_db

router = APIRouter(prefix="/admin", tags=["admin"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


@router.post("/requests/{request_id}/approve")
async def approve_request(
    request_id: int,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """通過 access request"""
    req = await db.get(AccessRequest, request_id)
    if not req:
        raise HTTPException(404, "Request not found")
    if req.status != "pending":
        raise HTTPException(400, f"Request already {req.status}")

    user = await db.get(User, req.user_id)
    if not user:
        raise HTTPException(404, "User not found")

    # Update request
    req.status = "approved"
    req.decided_by_id = admin.id
    req.decided_at = _now()

    # Update user
    user.status = "approved"
    user.decided_at = _now()
    user.decided_by_id = admin.id

    # Audit log
    await log_action(
        db,
        action="approve_request",
        user_id=admin.id,
        target=f"user:{user.email}",
        detail=f"request_id={request_id}",
    )

    await db.commit()
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/requests/{request_id}/reject")
async def reject_request(
    request_id: int,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    note: str = Form(""),
):
    """拒絕 access request"""
    req = await db.get(AccessRequest, request_id)
    if not req:
        raise HTTPException(404, "Request not found")
    if req.status != "pending":
        raise HTTPException(400, f"Request already {req.status}")

    user = await db.get(User, req.user_id)
    if not user:
        raise HTTPException(404, "User not found")

    req.status = "rejected"
    req.decided_by_id = admin.id
    req.decided_at = _now()
    req.decision_note = note or None

    user.status = "rejected"
    user.decided_at = _now()
    user.decided_by_id = admin.id

    await log_action(
        db,
        action="reject_request",
        user_id=admin.id,
        target=f"user:{user.email}",
        detail=f"request_id={request_id} note={note}",
    )

    await db.commit()
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/users/{user_id}/ban")
async def ban_user(
    user_id: int,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """ban user"""
    if user_id == admin.id:
        raise HTTPException(400, "Can't ban yourself")

    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")

    user.status = "banned"

    await log_action(
        db,
        action="ban_user",
        user_id=admin.id,
        target=f"user:{user.email}",
    )

    await db.commit()
    return RedirectResponse(url="/admin", status_code=303)