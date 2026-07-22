"""
【2026-07-22】Backfill `permission` column for existing users.

用法:
    cd /home/aping/MyProjects/TQark-web/backend
    uv run python scripts/migrate_add_permission.py

邏輯:
1. ALTER TABLE users ADD COLUMN permission INTEGER DEFAULT 9
2. 根據 status 反推 permission:
   - admin → 0
   - approved → 8
   - pending / rejected / banned → 9

Idempotent: 重跑不會壞 (SQLite 加 column 重複會 fail,但其他步驟 idempotent)
"""
import asyncio
import sys
from pathlib import Path

# 加入 backend 路徑
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, text

from app.db.models import User
from app.db.session import get_db, engine


async def migrate():
    print("=== Migrate add permission column ===\n")

    # 1. ALTER TABLE (SQLite 不支援 IF NOT EXISTS)
    async with engine.begin() as conn:
        try:
            await conn.execute(text("ALTER TABLE users ADD COLUMN permission INTEGER DEFAULT 9"))
            print("✓ ALTER TABLE users ADD COLUMN permission")
        except Exception as e:
            if "duplicate column" in str(e).lower():
                print("  (column already exists, skip ALTER)")
            else:
                raise

    # 2. Backfill
    STATUS_TO_PERMISSION = {
        "approved": 8,
        "pending": 9,
        "rejected": 9,
        "banned": 9,
    }

    migrated = 0
    async for db in get_db():
        result = await db.execute(select(User))
        users = result.scalars().all()

        for user in users:
            # admin 強制 = 0
            if user.role == "admin":
                if user.permission != 0:
                    user.permission = 0
                    migrated += 1
                    print(f"  [MIGRATE] {user.email}: permission -> 0 (admin)")
                continue

            # 一般 user 根據 status
            expected = STATUS_TO_PERMISSION.get(user.status, 9)
            if user.permission != expected:
                old = user.permission
                user.permission = expected
                migrated += 1
                print(f"  [MIGRATE] {user.email}: permission {old} -> {expected} (status={user.status})")

        await db.commit()
        print(f"\n✅ Done. Migrated {migrated} users.")
        break


if __name__ == "__main__":
    asyncio.run(migrate())
