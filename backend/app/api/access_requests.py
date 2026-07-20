"""
User access request endpoints

- POST /api/access-requests  → User 申請存取
"""

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db_helpers import log_action
from app.core.deps import require_login
from app.db.models import AccessRequest, User
from app.db.session import get_db

router = APIRouter(prefix="/api/access-requests", tags=["access-requests"])


@router.post("")
async def submit_request(
    reason: str = Form(...),
    user: User = Depends(require_login),
    db: AsyncSession = Depends(get_db),
):
    """
    User 申請 access,需要填 reason。
    如果之前已經有 pending request,擋掉(避免 spam)。
    """
    from sqlalchemy import select

    if not reason.strip():
        raise HTTPException(400, "Reason required")
    if user.status == "approved":
        raise HTTPException(400, "Already approved")
    if user.status == "banned":
        raise HTTPException(403, "Account is banned")

    # 【2026-07-20 修】擋重複 pending 申請
    # 同一個 user 如果已經有 pending request → 不能跳下一個
    # 防止 admin 後台被 spam 請求淹沒
    existing = (await db.execute(
        select(AccessRequest).where(
            AccessRequest.user_id == user.id,
            AccessRequest.status == "pending",
        )
    )).scalars().first()
    if existing:
        raise HTTPException(
            400,
            f"你已經有 pending 申請了(理由: {existing.reason[:50]}...)。請等 admin 審核。"
        )

    req = AccessRequest(
        user_id=user.id,
        reason=reason.strip(),
        status="pending",
    )
    db.add(req)
    user.application_reason = reason.strip()

    await log_action(
        db,
        action="submit_access_request",
        user_id=user.id,
        target=f"user:{user.email}",
        detail=f"reason={reason[:200]}",
    )

    await db.commit()
    return RedirectResponse(url="/dashboard", status_code=303)