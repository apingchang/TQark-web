"""
User access request endpoints

- POST /api/access-requests  → User 申請存取
"""

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db_helpers import log_action
from app.core.deps import require_login
from app.db.models import AccessRequest, User
from app.db.session import get_db

router = APIRouter(prefix="/api/access-requests", tags=["access-requests"])


def _dashboard_redirect(error: str, error_msg: str, success: str | None = None) -> RedirectResponse:
    """
    【2026-07-20 加】所有錯誤都重導回 /dashboard 帶 error param (不要 raise HTTPException)。
    原因:User 點表單 submit 是用 POST form 表單,如果 raise JSON 錯誤
    user 看到 raw JSON 而不是友善訊息。Redirect 回 dashboard 讓 template 顯示 alert。
    """
    qs = {
        "error": error,
        "error_msg": error_msg,
    }
    if success:
        qs["success"] = success
        qs["success_msg"] = "申請已送出!請等 admin 審核。"
    return RedirectResponse(url=f"/dashboard?{urlencode(qs)}", status_code=303)


@router.post("")
async def submit_request(
    reason: str = Form(...),
    user: User = Depends(require_login),
    db: AsyncSession = Depends(get_db),
):
    """
    User 申請 access,需要填 reason。
    如果之前已經有 pending request,擋掉(避免 spam)。

    【2026-07-20 改】所有錯誤改回 redirect + error query param (不 raise HTTPException)。
    原因:raw JSON 對表單 submit 的 user 不友善。
    """
    from sqlalchemy import select

    if not reason.strip():
        return _dashboard_redirect(
            "empty_reason",
            "申請原因不能空白,請填寫後重試。"
        )
    if user.status == "approved":
        return _dashboard_redirect(
            "already_approved",
            "你已經是 approved 狀態了,不需要重新申請。"
        )
    if user.status == "banned":
        return _dashboard_redirect(
            "banned",
            "你的帳號已被停用,無法申請。如有疑問請聯絡 admin。"
        )

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
        return _dashboard_redirect(
            "duplicate_pending",
            f"你已經有 pending 申請了(理由:{existing.reason[:50]}...),請等 admin 審核。"
        )

    req = AccessRequest(
        user_id=user.id,
        reason=reason.strip(),
        status="pending",
    )
    db.add(req)
    user.application_reason = reason.strip()

    # 【2026-07-22 改】送出申請 → permission 從 9 (register) 變 8 (pending)
    if user.permission == 9:
        user.permission = 8
        user.status = "pending"

    await log_action(
        db,
        action="submit_access_request",
        user_id=user.id,
        target=f"user:{user.email}",
        detail=f"reason={reason[:200]}",
    )

    await db.commit()

    # 成功也 redirect 帶 success param (可以顯示「申請已送出」)
    return _dashboard_redirect("", "", success="1")