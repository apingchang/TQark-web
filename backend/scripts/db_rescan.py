#!/usr/bin/env python
"""
Background DB full scan for TQark-web search (2026-08-17 新).

用途:
- Archive cron 完成後會 trigger incremental update (在 archive_multi_account.py)
- 此 script 是 6 小時保險掃描, 跑整個 archive folder → rebuild DB
- 設計: 即使 DB corrupt, 也能從 NAS 完整重建

用法:
    python scripts/db_rescan.py

Cron 範例:
    0 */6 * * *  cd /home/aping/MyProjects/TQark-web/backend && uv run python scripts/db_rescan.py
"""
import os
import sys
import time
from pathlib import Path

# 把 backend 加到 path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.scraper.db import init_db, rebuild_from_items, get_db_path
from app.scraper.local_index import _walk_archive

ARCHIVE_DIR = Path(os.environ.get("TQARK_ARCHIVE_DIR", "/mnt/my_book/考題收集"))


def main():
    db_path = get_db_path()
    print(f"[db_rescan] Archive: {ARCHIVE_DIR}", flush=True)
    print(f"[db_rescan] DB: {db_path}", flush=True)

    t0 = time.time()
    init_db(db_path)
    print(f"[db_rescan] init_db done ({time.time() - t0:.2f}s)", flush=True)

    t1 = time.time()
    items = _walk_archive()  # uses its own ARCHIVE_ROOT constant
    print(f"[db_rescan] walk archive: {len(items)} items ({time.time() - t1:.1f}s)", flush=True)

    t2 = time.time()
    rebuild_from_items(items, db_path=db_path)
    print(f"[db_rescan] rebuild done ({time.time() - t2:.1f}s)", flush=True)

    print(f"[db_rescan] ✅ Total: {time.time() - t0:.1f}s, {len(items)} items", flush=True)


if __name__ == "__main__":
    main()
