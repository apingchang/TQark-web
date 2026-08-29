#!/usr/bin/env python3
"""Fix rel_path for done records."""
import shutil
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from ocr_full_pipeline import build_new_relpath, build_new_filename, abs_from_rel, ARCHIVE_ROOT, DB_PATH
import sqlite3

conn = sqlite3.connect(DB_PATH, timeout=60)
conn.row_factory = sqlite3.Row

cur = conn.cursor()
cur.execute("""
    SELECT s.paper_id, s.county, s.school_name, s.school_year, s.school_term,
           s.exam_type, s.grade, s.subject,
           f.rel_path as current_path, f.paper_or_daan as filetype
    FROM ocr_status s
    JOIN files f ON s.paper_id = f.paper_id
    WHERE s.ocr_status = 'done'
""")
fixed = 0
for r in cur.fetchall():
    paper_id = r['paper_id']
    parsed = {
        'county': r['county'],
        'school_name': r['school_name'],
        'school_year': r['school_year'],
        'school_term': r['school_term'],
        'exam_type': r['exam_type'],
        'grade': r['grade'],
        'subject': r['subject'],
        'filetype': r['filetype'] or 'paper',
    }
    new_rel = build_new_relpath(parsed, filetype=parsed['filetype'])
    new_filename = build_new_filename(parsed, filetype=parsed['filetype'])
    # 確保 filename 結尾是 filetype (paper/daan)
    # 原本 format 是 _drivefolder.pdf, 應該改成 _paper.pdf 或 _daan.pdf
    # 但目前 build_new_filename 沒加 filetype suffix, 只在 rel_path 用
    # 修法: filename 結尾加 _paper.pdf 或 _daan.pdf
    
    new_abs = ARCHIVE_ROOT / new_rel
    old_abs = abs_from_rel(r['current_path'])
    
    if new_rel == r['current_path']:
        continue
    
    if new_abs.exists() and new_abs != old_abs:
        print(f'  {paper_id[:12]}: target exists, skip')
        continue
    
    if not old_abs:
        print(f'  {paper_id[:12]}: source not found: {r["current_path"]}')
        continue
    
    new_abs.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(old_abs), str(new_abs))
        cur.execute("UPDATE files SET rel_path = ? WHERE paper_id = ?", (new_rel, paper_id))
        conn.commit()
        fixed += 1
        print(f'  {paper_id[:12]}: moved')
    except OSError as e:
        print(f'  {paper_id[:12]}: move error: {e}')

print(f'\nFixed {fixed} records')
conn.close()
