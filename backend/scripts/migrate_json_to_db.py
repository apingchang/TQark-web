"""
從 local_papers_index.json 載入到 SQLite DB (2026-08-17 新)

【2026-08-17 新】第一次 migration 用, 之後用 incremental update
- 讀 local_papers_index.json
- 寫進 backend/state/tqark-web.db (用 db.rebuild_from_items)
- 備份原 JSON (rename)

執行:
    cd backend && .venv/bin/python scripts/migrate_json_to_db.py
"""
import json
import logging
import sys
from pathlib import Path

# 確保 backend/ 在 path
BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.scraper import db, local_index

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
_log = logging.getLogger("migrate")


JSON_PATH = Path("/mnt/my_book/考題收集/state/local_papers_index.json")
DB_PATH = db.DEFAULT_DB_PATH


def main():
    if not JSON_PATH.exists():
        _log.error(f"JSON not found: {JSON_PATH}")
        sys.exit(1)

    # 1. 備份 JSON
    bak = JSON_PATH.with_suffix(f".json.bak.{int(__import__('time').time())}")
    bak.write_bytes(JSON_PATH.read_bytes())
    _log.info(f"[MIGRATE] Backed up JSON to {bak}")

    # 2. 讀 JSON
    _log.info(f"[MIGRATE] Loading {JSON_PATH}")
    with open(JSON_PATH) as f:
        data = json.load(f)
    items = data.get("items", [])
    _log.info(f"[MIGRATE] Loaded {len(items)} items")

    # 3. 寫進 DB (備份現有 DB)
    if DB_PATH.exists():
        bak_db = db.backup_db()
        _log.info(f"[MIGRATE] Backed up DB to {bak_db}")
        db.clear_all()
    else:
        _log.info(f"[MIGRATE] No existing DB at {DB_PATH}, will create new")

    inserted, total = db.rebuild_from_items(items)
    _log.info(f"[MIGRATE] Inserted {inserted}/{total} items into DB")

    # 4. 驗證
    db_count = db.count_all()
    _log.info(f"[MIGRATE] DB count: {db_count}")
    if db_count != total:
        _log.warning(f"[MIGRATE] DB count {db_count} != expected {total}")

    _log.info(f"[MIGRATE] Done. DB at {DB_PATH}")
    _log.info(f"[MIGRATE] JSON backup at {bak}")
    _log.info(f"[MIGRATE] DB backup at {"N/A (new DB)" if not DB_PATH.exists() else bak_db}")


if __name__ == "__main__":
    main()
