"""
JWT 簽章 + 驗證

用 PyJWT 產生 + 驗證 access token(JWT)。
放在 httpOnly cookie 裡(`tqark_session`),前端 JS 拿不到。
"""

from datetime import datetime, timedelta, timezone

import jwt

from app.config import settings


def create_access_token(claims: dict) -> str:
    """
    給定 user claims(dict),產生 JWT。
    加上 exp (過期時間) + iat (簽發時間)。
    """
    now = datetime.now(timezone.utc)
    payload = {
        **claims,
        "iat": now,
        "exp": now + timedelta(days=settings.jwt_expire_days),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict | None:
    """
    驗證 JWT 並回傳 payload。
    失敗(過期、無效簽名等)就回 None,不丟 exception。
    """
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError:
        return None