"""
SQLAlchemy ORM models

Tables:
- users         — 從 Google 登入過的人(還沒 approve 的也算)
- access_requests — pending user 想申請 access 的單
- audit_log     — 所有重要動作的紀錄(login / approve / download / search)
- search_cache  — StudyArk 搜尋結果的 cache(降流量、省時間)
"""

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


def utcnow() -> datetime:
    """統一用 UTC 存時間"""
    return datetime.now(timezone.utc)


# ============================================================
# User
# ============================================================
class User(Base):
    """
    一個登入過 Google 的人。

    status:
    - pending   — 剛登入,還沒申請或申請審核中
    - approved  — admin 通過,可以搜尋/下載
    - rejected  — admin 拒絕
    - banned    — 之前 approved 但被 ban

    role:
    - user      — 一般使用者
    - admin     — 管理員(看 ADMIN_EMAILS env var)
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Google OAuth 給的資料
    google_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # sub
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    email_verified: Mapped[bool] = mapped_column(default=False)
    name: Mapped[str] = mapped_column(String(255), default="")
    picture: Mapped[str] = mapped_column(String(1024), default="")

    # 角色與狀態
    role: Mapped[str] = mapped_column(String(20), default="user")  # user / admin
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending/approved/rejected/banned

    # 時間
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)

    # 申請理由(從前端拿)
    application_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Reverse relationships
    access_requests: Mapped[list["AccessRequest"]] = relationship(
        back_populates="user",
        foreign_keys="AccessRequest.user_id",
    )

    def __repr__(self) -> str:
        return f"<User {self.email} [{self.role}/{self.status}]>"


# ============================================================
# AccessRequest
# ============================================================
class AccessRequest(Base):
    """
    User 想申請 access 的一筆紀錄(可以有多次申請,例如被拒後再申請)。
    """

    __tablename__ = "access_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    reason: Mapped[str] = mapped_column(Text)

    # 審核
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending/approved/rejected
    decided_by_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="access_requests", foreign_keys=[user_id])


# ============================================================
# AuditLog
# ============================================================
class AuditLog(Base):
    """
    所有重要動作的紀錄(login / approve / reject / search / download)。
    ip_hash = SHA256(ip + salt),不存明文 IP(保護 privacy)。
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)  # login/approve/reject/search/download/etc
    target: Mapped[str | None] = mapped_column(String(512), nullable=True)  # 被作用對象(例: exam_id)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON or free text

    ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


# ============================================================
# SearchCache
# ============================================================
class SearchCache(Base):
    """
    StudyArk 搜尋結果的 cache。
    key = 搜尋條件的 hash(或 canonical string)
    """

    __tablename__ = "search_cache"

    cache_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    results_json: Mapped[str] = mapped_column(Text)  # JSON-serialized list of exam metadata
    hit_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)