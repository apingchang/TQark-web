#!/usr/bin/env python3
"""
Stage 3 OCR Full Pipeline: OCR 全部 records (49,950+) 並 UPDATE DB

特點:
- ocr_status table: 追蹤每個 paper_id 狀態 (pending/done/failed/quarantined)
- Checkpoint: 每 100 records commit 一次
- Resume: 撈 ocr_status.status='pending' 從中斷點重跑
- Multi-process: ProcessPoolExecutor 加速
- Quarantine: 跟 DB 欄位差異過大 (county/school_name) 進 quarantine 不自動更新

執行模式:
- 全 DB 49,950+ records OCR + UPDATE → 8-15 hr
- 寫 log 到 /tmp/ocr_full_progress.log
- 可中斷重啟: 自動從 ocr_status 撈 pending 重跑

作者: 夥計 (William 指示) - 2026-08-26
"""
import argparse
import csv
import os
import sqlite3
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

# 從既有 OCR 模組 import
sys.path.insert(0, str(Path(__file__).parent))
from ocr_extract_metadata import (
    ocr_cover_robust, parse_ocr_text, ARCHIVE_ROOT, DB_PATH,
    quarantine_check,
)

LOG_PATH = Path('/tmp/ocr_full_progress.log')
PROGRESS_PATH = Path('/tmp/ocr_full_progress.csv')


def init_ocr_status_table(conn):
    """確保 ocr_status table 存在 (idempotent)"""
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ocr_status (
            paper_id        TEXT PRIMARY KEY,
            ocr_status      TEXT NOT NULL DEFAULT 'pending',
            county          TEXT,
            school_name     TEXT,
            school_year     TEXT,
            school_term     TEXT,
            exam_type       TEXT,
            grade           TEXT,
            subject         TEXT,
            ocr_text        TEXT,
            ocr_attempts    INTEGER DEFAULT 0,
            last_ocr_at     TEXT,
            quarantined_reason TEXT,
            created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at      TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ocr_status_status ON ocr_status(ocr_status)")
    conn.commit()


def seed_ocr_status(conn, only_drivefolder=False, limit=None):
    """從 files table 把 records seed 到 ocr_status (status='pending')"""
    cur = conn.cursor()
    where = []
    if only_drivefolder:
        where.append("rel_path LIKE '%_drivefolder%'")
    where_sql = 'WHERE ' + ' AND '.join(where) if where else ''
    cur.execute(f"""
        INSERT OR IGNORE INTO ocr_status (paper_id, ocr_status, created_at)
        SELECT paper_id, 'pending', CURRENT_TIMESTAMP FROM files
        {where_sql}
    """)
    inserted = cur.rowcount
    conn.commit()
    return inserted  # number of newly seeded records


def _abs_from_rel(rel: str) -> str:
    """從 rel_path 算 abs_path. 對 raw DriveFolder 自動加 _未分類/DriveFolder/ prefix.

    Strategy: 嘗試多個可能路徑, 返回第一個存在的
    """
    if not rel:
        return ''
    candidates = [
        ARCHIVE_ROOT / rel,  # 標準
        ARCHIVE_ROOT / '_未分類/DriveFolder' / rel,  # raw DriveFolder (rel_path 沒 _未分類 prefix)
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    # 都不存在 → return 第一個 (讓 abs_path missing error)
    return str(candidates[0])


def fetch_pending_records(conn, limit=None):
    """撈 ocr_status='pending' 的 paper_id + 對應 files 資訊. abs_path 從 rel_path 算."""
    cur = conn.cursor()
    limit_sql = f'LIMIT {limit}' if limit else ''
    cur.execute(f"""
        SELECT s.paper_id, f.rel_path, f.filename,
               f.county, f.school_name, f.school_year, f.school_term,
               f.exam_type, f.grade, f.subject, s.ocr_attempts
        FROM ocr_status s
        JOIN files f ON s.paper_id = f.paper_id
        WHERE s.ocr_status = 'pending'
        ORDER BY s.paper_id
        {limit_sql}
    """)
    rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        r['abs_path'] = _abs_from_rel(r.get('rel_path', ''))
    return rows


def ocr_one_record(record):
    """OCR + parse + 對比. Used by ProcessPoolExecutor. abs_path 從 rel_path 算."""
    abs_path = record.get('abs_path') or ''
    if not abs_path or not Path(abs_path).exists():
        abs_path = _abs_from_rel(record.get('rel_path', ''))
    
    if not abs_path or not Path(abs_path).exists():
        return {'paper_id': record['paper_id'], 'error': 'abs_path not file or not exists', 'parsed': {}}
    
    abs_p = Path(abs_path)
    if not abs_p.is_file():
        return {'paper_id': record['paper_id'], 'error': 'abs_path not file (可能是目錄)', 'parsed': {}}

    try:
        text, err = ocr_cover_robust(abs_p)
        if err:
            return {'paper_id': record['paper_id'], 'error': err, 'parsed': {}}
        
        parsed = parse_ocr_text(text)
        return {
            'paper_id': record['paper_id'],
            'parsed': parsed,
            'text': text[:500],
            'error': None,
        }
    except Exception as e:
        return {'paper_id': record['paper_id'], 'error': str(e), 'parsed': {}}


def update_record_status(conn, paper_id, status, parsed=None, ocr_text=None,
                         error=None, quarantined_reason=None, increment_attempt=False):
    """UPDATE ocr_status + 對比 diff 進 quarantine 或 done"""
    cur = conn.cursor()
    now = datetime.now().isoformat()
    if increment_attempt:
        cur.execute("UPDATE ocr_status SET ocr_attempts = ocr_attempts + 1, last_ocr_at = ? WHERE paper_id = ?", (now, paper_id))
    if status == 'failed':
        cur.execute("UPDATE ocr_status SET ocr_status = ?, last_ocr_at = ?, ocr_text = COALESCE(?, ocr_text) WHERE paper_id = ?",
                    (status, now, error, paper_id))
    elif status == 'done' and parsed:
        cur.execute("""
            UPDATE ocr_status SET
                ocr_status = ?, last_ocr_at = ?,
                county = COALESCE(NULLIF(?, ''), county),
                school_name = COALESCE(NULLIF(?, ''), school_name),
                school_year = COALESCE(NULLIF(?, ''), school_year),
                school_term = COALESCE(NULLIF(?, ''), school_term),
                exam_type = COALESCE(NULLIF(?, ''), exam_type),
                grade = COALESCE(NULLIF(?, ''), grade),
                subject = COALESCE(NULLIF(?, ''), subject),
                ocr_text = COALESCE(?, ocr_text)
            WHERE paper_id = ?
        """, (status, now,
              parsed.get('county', ''), parsed.get('school_name', ''),
              parsed.get('school_year', ''), parsed.get('school_term', ''),
              parsed.get('exam_type', ''), parsed.get('grade', ''),
              parsed.get('subject', ''), ocr_text, paper_id))
    elif status == 'quarantined':
        cur.execute("""
            UPDATE ocr_status SET
                ocr_status = ?, last_ocr_at = ?, quarantined_reason = ?,
                ocr_text = COALESCE(?, ocr_text)
            WHERE paper_id = ?
        """, (status, now, quarantined_reason, ocr_text, paper_id))
    conn.commit()


def check_and_quarantine(conn, record, parsed):
    """對比 OCR vs DB. 大差異 → quarantine. 小差異 → done (DB 已被 UPDATE)."""
    if not parsed:
        return 'failed', 'parse empty'
    
    # 算 diffs
    diffs = quarantine_check(parsed, record)
    if not diffs:
        return 'done', None
    
    # 判斷 quarantine 規則: county 或 school_name 完全不同 → quarantine
    critical_diff = {}
    for k in ['county', 'school_name']:
        if k in diffs:
            critical_diff[k] = diffs[k]
    
    if critical_diff:
        # 大差異 → quarantine (不更新 DB, 等人工 review)
        reason = '; '.join(f"{k}: db='{d['db']}' ocr='{d['ocr']}'" for k, d in critical_diff.items())
        return 'quarantined', reason
    else:
        # 小差異 (term/exam/year/grade/subject) → done (DB 已 UPDATE)
        return 'done', None


def main():
    parser = argparse.ArgumentParser(description='Stage 3 OCR full pipeline')
    parser.add_argument('--mode', choices=['seed', 'run', 'apply'], default='run',
                        help='seed: init ocr_status; run: OCR + update; apply: apply ocr_status → files DB')
    parser.add_argument('--only-drivefolder', action='store_true', help='only _drivefolder records')
    parser.add_argument('--limit', type=int, default=None, help='limit records (for testing)')
    parser.add_argument('--workers', type=int, default=4, help='parallel workers')
    parser.add_argument('--batch-size', type=int, default=50, help='commit every N records')
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH, timeout=120)
    conn.row_factory = sqlite3.Row
    
    # 1. ensure table exists
    init_ocr_status_table(conn)

    if args.mode == 'seed':
        # Seed records to ocr_status
        inserted = seed_ocr_status(conn, only_drivefolder=args.only_drivefolder, limit=args.limit)
        print(f'[seed] Inserted {inserted} records into ocr_status')
        conn.close()
        return 0

    # 2. fetch pending records
    pending = fetch_pending_records(conn, limit=args.limit)
    print(f'[run] Pending records: {len(pending)}, workers: {args.workers}, batch: {args.batch_size}')

    if not pending:
        print('[run] No pending records. Run --mode seed first.')
        conn.close()
        return 0

    # 3. OCR in parallel
    stats = Counter()
    log_lines = []
    t0 = time.time()
    
    with open(LOG_PATH, 'a') as logf:
        logf.write(f'\n[{datetime.now().isoformat()}] === run start: {len(pending)} records, {args.workers} workers ===\n')
        logf.flush()
        
        with ProcessPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(ocr_one_record, r): r for r in pending}
            
            for i, f in enumerate(as_completed(futures)):
                record = futures[f]
                paper_id = record['paper_id']
                
                res = f.result()
                err = res.get('error')
                parsed = res.get('parsed', {})
                text = res.get('text', '')
                
                if err:
                    stats['error'] += 1
                    update_record_status(conn, paper_id, 'failed', error=err, increment_attempt=True)
                elif not parsed:
                    stats['empty_parse'] += 1
                    update_record_status(conn, paper_id, 'failed', error='parse empty', increment_attempt=True)
                else:
                    status, reason = check_and_quarantine(conn, record, parsed)
                    update_record_status(conn, paper_id, status, parsed=parsed, ocr_text=text,
                                          quarantined_reason=reason, increment_attempt=True)
                    stats[status] += 1
                    log_lines.append(f'{paper_id[:12]},{status},{reason or ""},{record.get("rel_path","")[:60]}')
                
                if (i + 1) % args.batch_size == 0:
                    elapsed = time.time() - t0
                    rate = (i + 1) / elapsed if elapsed > 0 else 0
                    eta = (len(pending) - i - 1) / rate if rate > 0 else 0
                    log_msg = f'  [{i+1}/{len(pending)}] {stats} {rate:.1f}/s ETA {eta:.0f}s'
                    print(log_msg)
                    logf.write(log_msg + '\n')
                    logf.flush()
                    with open(PROGRESS_PATH, 'a') as pf:
                        pf.write('\n'.join(log_lines) + '\n')
                        log_lines = []
    
    elapsed = time.time() - t0
    print(f'\n[run] Done in {elapsed:.1f}s ({len(pending)/elapsed:.1f}/s)')
    print(f'[run] Stats: {dict(stats)}')
    with open(LOG_PATH, 'a') as logf:
        logf.write(f'[{datetime.now().isoformat()}] === run done: {elapsed:.1f}s {dict(stats)} ===\n')
    
    print(f'\n[run] Progress log: {LOG_PATH}')
    print(f'[run] Next step: --mode apply (apply ocr_status → files DB)')
    
    conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())


# ============================================================
# apply mode: 對 quarantined / done records 套用 ocr_status 到 files DB
# (對 quarantined 人工 review 完之後跑)
# ============================================================

def apply_ocr_to_files_db(conn, only_status=None):
    """從 ocr_status UPDATE 到 files DB. 預設 status='done' 不覆蓋 quarantine."""
    cur = conn.cursor()
    where = ''
    params = []
    if only_status:
        where = 'WHERE s.ocr_status = ?'
        params = [only_status]
    
    cur.execute(f"""
        SELECT s.paper_id, s.county, s.school_name, s.school_year,
               s.school_term, s.exam_type, s.grade, s.subject, s.ocr_status
        FROM ocr_status s
        JOIN files f ON s.paper_id = f.paper_id
        {where}
    """, params)
    
    updated = 0
    for r in cur.fetchall():
        cur.execute("""
            UPDATE files SET
                county = COALESCE(NULLIF(?, ''), county),
                school_name = COALESCE(NULLIF(?, ''), school_name),
                school_year = COALESCE(NULLIF(?, ''), school_year),
                school_term = COALESCE(NULLIF(?, ''), school_term),
                exam_type = COALESCE(NULLIF(?, ''), exam_type),
                grade = COALESCE(NULLIF(?, ''), grade),
                subject = COALESCE(NULLIF(?, ''), subject)
            WHERE paper_id = ?
        """, (r['county'], r['school_name'], r['school_year'], r['school_term'],
              r['exam_type'], r['grade'], r['subject'], r['paper_id']))
        updated += cur.rowcount
    conn.commit()
    return updated
