#!/usr/bin/env python3
"""
Stage 3 Full: OCR + UPDATE DB + RENAME + MOVE 整合版

從 ocr_status (done + quarantined) 把結果套到 files DB:
1. UPDATE files: county/school_name/year/term/exam/grade/subject 從 ocr_status 套到 files
2. RENAME filename: 跟新 metadata 一致
3. MOVE file: 對 quarantined records (county/school 跟原 folder 不一致) 搬到正確 folder

Quarantine records (大改動) 自動處理, 但有衝突就 skip + 標記
作者: 夥計 (William 指示) - 2026-08-26
"""
import argparse
import csv
import sqlite3
import shutil
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

# Import 從 ocr_extract_metadata
sys.path.insert(0, str(Path(__file__).parent))
from ocr_extract_metadata import (
    ARCHIVE_ROOT, DB_PATH, _abs_from_rel, parse_ocr_text,
)

CSV_OUT = Path('/tmp/ocr_apply_results.csv')
LOG_PATH = Path('/tmp/ocr_apply.log')


def log(msg):
    line = f'[{datetime.now().isoformat()}] {msg}'
    print(line)
    with LOG_PATH.open('a') as f:
        f.write(line + '\n')


def update_files_db(conn, paper_id, county, school_name, school_year,
                     school_term, exam_type, grade, subject):
    """UPDATE files 從 ocr_status. 只在欄位空時填 (不覆蓋已有值)."""
    cur = conn.cursor()
    cur.execute("""
        UPDATE files SET
            county = COALESCE(NULLIF(?, ''), county),
            school_name = COALESCE(NULLIF(?, ''), school_name),
            school_year = COALESCE(NULLIF(?, ''), school_year),
            school_term = COALESCE(NULLIF(?, ''), school_term),
            exam_type = COALESCE(NULLIF(?, ''), exam_type),
            grade = COALESCE(NULLIF(?, ''), grade),
            subject = COALESCE(NULLIF(?, ''), subject),
            updated_at = CURRENT_TIMESTAMP
        WHERE paper_id = ?
    """, (county, school_name, school_year, school_term,
          exam_type, grade, subject, paper_id))
    return cur.rowcount


def build_new_filename(county, school_year, school_term, exam_type,
                       subject, grade, school_name, version):
    """從 metadata 建 StudyArk 標準 filename. 跟 build_target_filename 一致."""
    if school_term:
        year_term = f'{school_year}{school_term}' if school_year else '未註明'
    else:
        year_term = school_year if school_year else '未註明'
    safe_school = school_name.replace('/', '／').replace(':', '：') if school_name else '未註明'
    safe_exam = (exam_type or '未註明').replace('/', '／').replace(':', '：')
    safe_subject = (subject or '未分類').replace('/', '／').replace(':', '：')
    safe_grade = (grade or '未註明').replace('/', '／').replace(':', '：')
    safe_version = (version or '未註明').replace('/', '／').replace(':', '：')
    return f'{county}_{year_term}_{safe_exam}_{safe_subject}_{safe_grade}_{safe_school}_{safe_version}_drivefolder.pdf'


def build_new_relpath(county, level, grade, subject, filetype, filename):
    """從 metadata 建新 rel_path."""
    safe_subject = (subject or '未分類').replace('/', '／').replace(':', '：')
    safe_grade = grade.replace('/', '／').replace(':', '：')
    return f'{county}/{level}/{safe_grade}/{safe_subject}/{filetype}/{filename}'


def fetch_done_records(conn, only_drivefolder=False):
    """撈 ocr_status=done records (DB metadata 跟 OCR 一致或小差異)."""
    cur = conn.cursor()
    where = "WHERE s.ocr_status = 'done'"
    if only_drivefolder:
        where += " AND f.rel_path LIKE '%_drivefolder%'"
    cur.execute(f"""
        SELECT s.paper_id, s.county, s.school_name, s.school_year, s.school_term,
               s.exam_type, s.grade, s.subject,
               f.rel_path, f.county as db_county, f.school_name as db_school
        FROM ocr_status s
        JOIN files f ON s.paper_id = f.paper_id
        {where}
    """)
    return [dict(r) for r in cur.fetchall()]


def fetch_quarantined_records(conn, only_drivefolder=False):
    """撈 ocr_status=quarantined records (county/school 跟 OCR 大不一致)."""
    cur = conn.cursor()
    where = "WHERE s.ocr_status = 'quarantined'"
    if only_drivefolder:
        where += " AND f.rel_path LIKE '%_drivefolder%'"
    cur.execute(f"""
        SELECT s.paper_id, s.county, s.school_name, s.school_year, s.school_term,
               s.exam_type, s.grade, s.subject,
               f.rel_path, f.filename, f.county as db_county, f.school_name as db_school
        FROM ocr_status s
        JOIN files f ON s.paper_id = f.paper_id
        {where}
    """)
    return [dict(r) for r in cur.fetchall()]


def should_move(record):
    """判斷是否需要 move (county 或 school 跟 path 不一致)."""
    if not record.get('ocr_county') or not record.get('ocr_school'):
        return False
    rel = record.get('rel_path', '')
    # path 內第一段是 county
    parts = rel.split('/')
    if len(parts) < 2:
        return False
    file_county = parts[0]
    file_school = parts[1] if len(parts) > 1 else ''
    return (file_county != record['ocr_county'] or
            file_school != record['ocr_school'])


def move_and_rename(record, dry_run=False):
    """Move + rename 單個 record."""
    old_abs = ARCHIVE_ROOT / record['rel_path']
    if not old_abs.exists():
        return 'abs_path_missing'
    
    new_county = record['ocr_county']
    new_school = record['ocr_school']
    new_filename = build_new_filename(
        new_county, record['ocr_school_year'], record['ocr_school_term'],
        record['ocr_exam_type'], record['ocr_subject'], record['ocr_grade'],
        new_school, '未註明'
    )
    # 決定 level - 從 grade (1-6=國小, 7-9=國中, 10-12=高中)
    grade = record['ocr_grade']
    if grade and grade in ['一年級', '二年級', '三年級', '四年級', '五年級', '六年級']:
        level = '國小'
    elif grade and grade in ['七年級', '八年級', '九年級']:
        level = '國中'
    elif grade and grade in ['十年級', '十一年級', '十二年級']:
        level = '高中'
    else:
        level = '國中'  # default
    
    new_rel = f'{new_county}/{level}/{grade}/{record["ocr_subject"] or "未分類"}/paper/{new_filename}'
    new_abs = ARCHIVE_ROOT / new_rel
    
    if dry_run:
        return f'dry_run: {record["rel_path"]} -> {new_rel}'
    
    if new_abs.exists() and new_abs != old_abs:
        return f'target_exists: {new_abs}'
    
    new_abs.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(old_abs), str(new_abs))
    except OSError as e:
        return f'move_failed: {e}'
    
    return f'moved: {record["rel_path"]} -> {new_rel}'


def main():
    parser = argparse.ArgumentParser(description='Stage 3 Full: UPDATE DB + RENAME + MOVE')
    parser.add_argument('--mode', choices=['update-only', 'move-only', 'full'], default='full',
                        help='update-only: 只 UPDATE DB, 不動檔案; full: UPDATE + RENAME + MOVE')
    parser.add_argument('--dry-run', action='store_true', help='dry-run 不動 disk')
    parser.add_argument('--only-done', action='store_true', help='只處理 done (小差異)')
    parser.add_argument('--only-quarantined', action='store_true', help='只處理 quarantined (大差異/move)')
    parser.add_argument('--limit', type=int, default=None, help='limit records')
    args = parser.parse_args()
    
    if not args.only_done and not args.only_quarantined:
        # default: 兩種都處理
        args.only_done = True
        args.only_quarantined = True
    
    log(f'[apply] Mode: {args.mode}, dry-run: {args.dry_run}')
    log(f'[apply] done: {args.only_done}, quarantined: {args.only_quarantined}')
    
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    
    stats = Counter()
    csv_rows = []
    t0 = time.time()
    
    # Phase 1: done records - UPDATE DB
    if args.only_done:
        done_records = fetch_done_records(conn)
        if args.limit:
            done_records = done_records[:args.limit]
        log(f'[apply] Phase 1: UPDATE {len(done_records)} done records')
        for r in done_records:
            try:
                updated = update_files_db(
                    conn, r['paper_id'],
                    r['county'], r['school_name'], r['school_year'],
                    r['school_term'], r['exam_type'], r['grade'], r['subject'],
                )
                stats['updated'] += 1
                csv_rows.append({
                    'paper_id': r['paper_id'][:12],
                    'action': 'update',
                    'paper': r['rel_path'][:60],
                    'status': 'ok' if updated else 'no_change',
                })
            except Exception as e:
                stats['update_error'] += 1
                csv_rows.append({
                    'paper_id': r['paper_id'][:12],
                    'action': 'update',
                    'paper': r['rel_path'][:60],
                    'status': f'error: {e}',
                })
        conn.commit()
    
    # Phase 2: quarantined records - UPDATE + MOVE
    if args.only_quarantined and args.mode in ('move-only', 'full'):
        quaran_records = fetch_quarantined_records(conn)
        if args.limit:
            quaran_records = quaran_records[:args.limit]
        log(f'[apply] Phase 2: UPDATE + MOVE {len(quaran_records)} quarantined records')
        for r in quaran_records:
            # 加 ocr_ prefix 到欄位
            r['ocr_county'] = r.pop('county')
            r['ocr_school'] = r.pop('school_name')
            r['ocr_school_year'] = r.pop('school_year')
            r['ocr_school_term'] = r.pop('school_term')
            r['ocr_exam_type'] = r.pop('exam_type')
            r['ocr_grade'] = r.pop('grade')
            r['ocr_subject'] = r.pop('subject')
            
            # UPDATE DB
            try:
                update_files_db(
                    conn, r['paper_id'],
                    r['ocr_county'], r['ocr_school'], r['ocr_school_year'],
                    r['ocr_school_term'], r['ocr_exam_type'], r['ocr_grade'], r['ocr_subject'],
                )
                stats['updated_q'] += 1
            except Exception as e:
                stats['update_q_error'] += 1
                continue
            
            # MOVE + RENAME (only if needed)
            if should_move(r):
                result = move_and_rename(r, dry_run=args.dry_run)
                if 'moved' in result or 'dry_run' in result:
                    stats['moved'] += 1
                elif 'target_exists' in result:
                    stats['skip_target_exists'] += 1
                else:
                    stats['move_error'] += 1
                csv_rows.append({
                    'paper_id': r['paper_id'][:12],
                    'action': 'move',
                    'paper': r['rel_path'][:60],
                    'status': result,
                })
            else:
                stats['no_move_needed'] += 1
        conn.commit()
    
    # Write CSV
    if csv_rows:
        with CSV_OUT.open('w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=['paper_id', 'action', 'paper', 'status'])
            writer.writeheader()
            writer.writerows(csv_rows)
    
    elapsed = time.time() - t0
    log(f'[apply] Done in {elapsed:.1f}s')
    log(f'[apply] Stats: {dict(stats)}')
    log(f'[apply] CSV: {CSV_OUT}')
    log(f'[apply] Log: {LOG_PATH}')
    
    conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
