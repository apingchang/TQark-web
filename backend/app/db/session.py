"""
SQLAlchemy database session

- 用 aiosqlite 給 FastAPI async 用
- Engine + Session factory
- get_db dependency for FastAPI
"""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


# SQLite 需要這個 connect_args(讓多個 thread 都能用同一個 connection)
DATABASE_URL = f"sqlite+aiosqlite:///{settings.db_path}"

engine = create_async_engine(
    DATABASE_URL,
    echo=False,  # 之後可以設 True 看 SQL
    connect_args={"check_same_thread": False, "timeout": 30},
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    """所有 ORM model 的 base class"""
    pass


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency,每個 request 拿到一個 session"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """啟動時建 table(開發階段用,production 應該用 Alembic)"""
    # 確保 DB file 的資料夾存在
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)

    # 延遲 import 避免循環依賴
    from app.db.models import User  # noqa: F401

    # checkfirst=True + SQLite busy_timeout 避免多 worker 同時建 table 衝突
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, checkfirst=True)