"""
FastAPI dependencies

- get_current_user:從 cookie 解 JWT,查 DB 拿 user object
- require_admin:確認是 admin
- require_approved:確認 status=approved
"""

from fastapi import Cookie, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import SESSION_COOKIE
from app.config import settings
from app.core.security import decode_access_token
from app.db.models import User
from app.db.session import get_db


async def get_current_user_from_token(
    tqark_session: str | None = Cookie(default=None),
    db: AsyncSession = Depends(get_db),
) -> User | None:
    """
    從 cookie 解 JWT,然後查 DB 拿 user object。
    沒有 cookie / token 過期 / user 不存在 → 回傳 None(不丟 exception,讓 caller 決定)
    """
    if not tqark_session:
        return None

    claims = decode_access_token(tqark_session)
    if not claims:
        return None

    uid = claims.get("uid")
    if not uid:
        return None

    user = await db.get(User, uid)
    return user


async def require_login(
    user: User | None = Depends(get_current_user_from_token),
) -> User:
    """必須登入,沒登入就 401"""
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not logged in",
            headers={"Location": "/auth/google/login"},
        )
    return user


async def require_admin(
    user: User = Depends(require_login),
) -> User:
    """必須是 admin"""
    if user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin only")
    return user


async def require_approved(
    user: User = Depends(require_login),
) -> User:
    """
    【2026-07-22 改】用 permission 取代 status 判斷。
    - permission <= 8 表示 user 通過審核 (8=approved, <8 是更高權限 / admin)
    - permission == 9 表示 pending (不能下載)
    - admin (permission=0) 也能進 (因為 0 < 8)
    """
    if user.permission > 8:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"Permission {user.permission}: need <= 8 (admin=0, approved=8, pending=9)",
        )
    return user