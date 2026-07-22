"""
DB 操作 helpers

把 user / audit log 的常見操作抽成 function,auth.py / admin.py 共用。
"""

import hashlib

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog, User


# Salt for IP hashing(避免直接存明文 IP)
_IP_SALT = "tqark-web-ip-salt-2026"


def hash_ip(ip: str) -> str:
    """SHA256(ip + salt),不存明文 IP"""
    return hashlib.sha256(f"{_IP_SALT}:{ip}".encode()).hexdigest()[:32]


async def upsert_user(db: AsyncSession, userinfo: dict, admin_emails: list[str]) -> User:
    """
    從 Google userinfo 建或更新 User。
    - 如果 google_id 已存在:更新 name / picture / last_login_at
    - 如果不存在:建一個,status=pending,role 從 admin_emails 判斷
    """
    google_id = userinfo.get("sub")
    email = userinfo.get("email", "")
    email_lower = email.lower()

    is_admin = email_lower in [e.lower() for e in admin_emails]

    # 找現有的 user(用 google_id 或 email)
    stmt = select(User).where(User.google_id == google_id)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if user is None:
        # 試 email 也找一下(防同個人用不同 google_id 登入)
        stmt2 = select(User).where(User.email == email)
        result2 = await db.execute(stmt2)
        user = result2.scalar_one_or_none()

    from app.db.models import utcnow

    if user is None:
        # 建新 user
        user = User(
            google_id=google_id,
            email=email,
            email_verified=userinfo.get("email_verified", False),
            name=userinfo.get("name", ""),
            picture=userinfo.get("picture", ""),
            role="admin" if is_admin else "user",
            # admin 預設就是 approved,一般 user 預設 register (剛登入、還沒申請)
            status="approved" if is_admin else "pending",
            # 【2026-07-22 改】permission 往下移一階:
            #   admin = 0
            #   approved = 7
            #   pending = 8
            #   register = 9 (剛 Google 登入,還沒申請)
            permission=0 if is_admin else 9,
            first_seen_at=utcnow(),
            last_login_at=utcnow(),
        )
        db.add(user)
    else:
        # 更新
        user.email_verified = userinfo.get("email_verified", False)
        user.name = userinfo.get("name", "")
        user.picture = userinfo.get("picture", "")
        user.last_login_at = utcnow()
        # 如果之前不是 admin,現在被加進 admin_emails,升級
        if is_admin and user.role != "admin":
            user.role = "admin"
            user.status = "approved"
            user.permission = 0  # admin permission = 0

    await db.flush()
    return user


async def log_action(
    db: AsyncSession,
    action: str,
    user_id: int | None = None,
    target: str | None = None,
    detail: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    """寫一筆 audit log"""
    log = AuditLog(
        user_id=user_id,
        action=action,
        target=target,
        detail=detail,
        ip_hash=hash_ip(ip) if ip else None,
        user_agent=user_agent[:512] if user_agent else None,
    )
    db.add(log)
    await db.flush()