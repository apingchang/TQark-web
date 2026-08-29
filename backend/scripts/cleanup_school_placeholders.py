#!/usr/bin/env python3
"""清理 DB 內 school_name 內的 placeholder 文字 + 開頭重複 county."""
import re
import sqlite3

DB_PATH = '/home/aping/MyProjects/TQark-web/backend/state/tqark-web.db'
conn = sqlite3.connect(DB_PATH, timeout=60)

cur = conn.cursor()
cur.execute("""
    SELECT paper_id, school_name, county
    FROM files
    WHERE school_name LIKE '%local_%'
       OR school_name LIKE '%unknown_%'
       OR school_name LIKE '%_新北市_新北市%'  -- 重複 county
""")
fixed = 0
for paper_id, school, county in cur.fetchall():
    if not school:
        continue
    cleaned = school
    # 移除 _local_xxx_ 或 _unknown_xxx_ (有 _ 包夾)
    cleaned = re.sub(r'_(?:local_[a-z0-9_]+|unknown_[^_]+)_', '_', cleaned)
    # 移除結尾的 _local_xxx 或 _unknown_xxx
    cleaned = re.sub(r'_(?:local_[a-z0-9_]+|unknown_[^_]+)$', '', cleaned)
    # 移除開頭重複的 county: "新北市新北市崇林國中" → "新北市崇林國中"
    if county and cleaned.startswith(county + county):
        cleaned = cleaned[len(county):]
    # 移除多餘的底線
    cleaned = re.sub(r'_+', '_', cleaned).strip('_')
    if cleaned != school:
        cur.execute("UPDATE files SET school_name = ? WHERE paper_id = ?", (cleaned, paper_id))
        fixed += 1
        if fixed <= 5:
            print(f'  {paper_id[:12]}: {school} -> {cleaned}')

conn.commit()
print(f'\nFixed {fixed} school_name records')
conn.close()
