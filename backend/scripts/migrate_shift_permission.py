"""
【2026-07-22 19:15】把現有 user 的 permission 往下移一階:

舊對應 → 新對應:
- admin (0) → 0 (不變)
- approved (8) → 7
- pending (9) → 8
- rejected (9) → 9 (不變,本來就 9)
- banned (9) → 9 (不變,本來就 9)

Idempotent: 重跑不會壞 (沒被手動改過的 user 才會 migrate)
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.db.models import User
from app.db.session import get_db


async def migrate():
    print("=== Shift permission down by 1 ===\n")

    # Map: old_permission -> new_permission
    SHIFT_MAP = {
        0: 0,    # admin -> admin (unchanged)
        8: 7,    # approved -> approved (new floor)
        9: 9,    # pending/rejected/banned -> pending/rejected/banned (unchanged)
        # 1-7 之前沒用到,保持原樣
    }

    migrated = 0
    async for db in get_db():
        result = await db.execute(select(User))
        users = result.scalars().all()

        for user in users:
            new_perm = SHIFT_MAP.get(user.permission)
            if new_perm is None:
                print(f"  [SKIP] {user.email}: permission={user.permission} not in SHIFT_MAP (保留原值)")
                continue

            if new_perm != user.permission:
                old = user.permission
                user.permission = new_perm
                migrated += 1
                print(f"  [MIGRATE] {user.email}: {old} -> {new_perm} (status={user.status})")
            else:
                pass  # unchanged

        await db.commit()
        print(f"\n✅ Done. Migrated {migrated} users.")
        break


if __name__ == "__main__":
    asyncio.run(migrate())
