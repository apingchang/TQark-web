"""
Pytest configuration for TQark-web tests (2026-08-17 新).

主要功能:
- 提供 module-scope login token (避免每個 test 重複 async 登入)
- 設定 sys.path 確保 backend import 可用
"""
import asyncio
import os
import sys
from pathlib import Path

# 把 backend 加到 path
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))


def _get_token() -> str:
    """從 DB 拿 admin user id, generate JWT token (sync 包裝)."""
    from sqlalchemy import text
    from app.db.session import AsyncSessionLocal
    from app.core.security import create_access_token

    loop = asyncio.new_event_loop()
    try:
        async def _get():
            async with AsyncSessionLocal() as d:
                r = await d.execute(text("SELECT id FROM users WHERE email='apingchang@gmail.com'"))
                uid = r.scalar()
                return create_access_token({"uid": str(uid), "email": "apingchang@gmail.com"})
        return loop.run_until_complete(_get())
    finally:
        loop.close()


import pytest


@pytest.fixture(scope="session")
def admin_token():
    """session-scope login token (one-time generation)"""
    return _get_token()
