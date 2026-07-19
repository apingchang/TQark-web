"""
Google OAuth 2.0 client

用 authlib 的 OAuth client 處理:
1. /auth/google/login → 跳轉到 Google 同意畫面
2. /auth/google/callback → Google redirect 回來,我們拿 code 換 token
3. 拿 user info(email, name, picture, sub)
"""

from authlib.integrations.httpx_client import AsyncOAuth2Client

from app.config import settings


GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_ACCESS_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


def make_oauth_client() -> AsyncOAuth2Client:
    """建一個 OAuth client 實例,每個 request 一個。"""
    return AsyncOAuth2Client(
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        scope="openid email profile",
        redirect_uri=f"{settings.public_base_url}/auth/google/callback",
    )


async def fetch_google_userinfo(access_token: str) -> dict:
    """
    用 access_token 去 Google userinfo endpoint 拿使用者資料。
    回傳 dict 至少有: sub, email, email_verified, name, picture
    """
    async with AsyncOAuth2Client() as client:
        resp = await client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        return resp.json()