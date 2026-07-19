"""
Google OAuth + JWT 認證 endpoints

Endpoints:
- GET  /auth/google/login      → 302 跳轉到 Google 同意畫面
- GET  /auth/google/callback   → Google redirect 回來,我們處理 code
- POST /auth/logout            → 刪 cookie
- GET  /auth/me                → 看現在的 user 資料(從 cookie 解 JWT)
"""

import secrets
from urllib.parse import urlencode

from fastapi import APIRouter, Cookie, HTTPException, Response
from fastapi.responses import RedirectResponse

from app.config import settings
from app.core.oauth import fetch_google_userinfo, make_oauth_client
from app.core.security import create_access_token, decode_access_token

router = APIRouter(prefix="/auth", tags=["auth"])

# Cookie 名稱
SESSION_COOKIE = "tqark_session"


# ============================================================
# OAuth flow state(簡單版,production 應該用 Redis 之類的)
# ============================================================
# Phase 1 暫時用記憶體存 state,服務重啟就清掉。
# Production 應該用 Redis / DB 存 + 設定 TTL。
_state_store: dict[str, bool] = {}


@router.get("/google/login")
async def google_login():
    """
    1. 生 CSRF state token
    2. 存起來(session cookie 或 server-side store)
    3. 跳轉到 Google 同意畫面
    """
    state = secrets.token_urlsafe(32)
    _state_store[state] = True

    # 建 Google OAuth authorize URL
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": f"{settings.public_base_url}/auth/google/callback",
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
    }
    google_url = f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"
    return RedirectResponse(url=google_url)


@router.get("/google/callback")
async def google_callback(code: str | None = None, state: str | None = None, error: str | None = None):
    """
    Google redirect 回來,帶 code + state。
    我們:
    1. 驗 state(防 CSRF)
    2. 用 code 換 access_token
    3. 拿 userinfo
    4. 產 JWT,設 cookie
    5. 跳回首頁
    """
    if error:
        raise HTTPException(400, f"Google OAuth error: {error}")
    if not code or not state:
        raise HTTPException(400, "Missing code or state")

    # 1. 驗 state
    if _state_store.pop(state, None) is None:
        raise HTTPException(400, "Invalid state (可能的 CSRF 攻擊)")
    # ↑ pop() 一次用掉,防 replay

    # 2. 用 code 換 access_token
    client = make_oauth_client()
    token = await client.fetch_access_token(
        "https://oauth2.googleapis.com/token",
        code=code,
    )
    access_token = token.get("access_token")
    if not access_token:
        raise HTTPException(400, f"Failed to get access token: {token}")

    # 3. 拿 userinfo
    userinfo = await fetch_google_userinfo(access_token)
    email = userinfo.get("email")
    if not email:
        raise HTTPException(400, "Google 沒回 email(可能 user 沒授權)")

    # 4. 查 / 建 user(DB)
    from app.db.session import get_db
    from app.db.models import User
    from app.core.db_helpers import upsert_user, log_action

    async for db in get_db():
        user = await upsert_user(db, userinfo, settings.admin_email_list)
        await log_action(
            db,
            user_id=user.id,
            action="login",
            target=email,
            detail=f"is_admin={user.role == 'admin'}",
        )
        await db.commit()

    # 5. 產 JWT claims
    claims = {
        "sub": user.google_id,
        "uid": user.id,  # DB primary key
        "email": email,
        "email_verified": user.email_verified,
        "name": user.name,
        "picture": user.picture,
        "is_admin": user.role == "admin",
        "status": user.status,  # pending / approved / rejected / banned
    }
    jwt_token = create_access_token(claims)

    # 5. 設 cookie + 跳回首頁
    #    - HttpOnly: 前端 JS 拿不到,防 XSS
    #    - Secure: 只在 HTTPS 傳(我們 reverse proxy 是 HTTPS)
    #    - SameSite=Lax: 防 CSRF
    #    - Max-Age: 14 天
    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=jwt_token,
        max_age=settings.jwt_expire_days * 24 * 60 * 60,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return response


@router.post("/logout")
async def logout(response: Response):
    """刪 session cookie"""
    response.delete_cookie(key=SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
async def me(tqark_session: str | None = Cookie(default=None)):
    """
    看現在登入的人是誰。
    從 cookie 解 JWT,回傳 user claims(沒有 DB 查詢,Phase 1 簡化版)。
    """
    if not tqark_session:
        return {"authenticated": False}

    claims = decode_access_token(tqark_session)
    if not claims:
        return {"authenticated": False, "reason": "invalid or expired token"}

    return {
        "authenticated": True,
        "email": claims.get("email"),
        "name": claims.get("name"),
        "picture": claims.get("picture"),
        "is_admin": claims.get("is_admin", False),
    }