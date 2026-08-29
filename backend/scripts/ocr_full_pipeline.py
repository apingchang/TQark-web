import re
'\nStage 3 Streaming Pipeline: OCR 一個 file 就 UPDATE DB + RENAME + MOVE\n\n設計:\n- 不分 seed / run / apply 階段\n- 撈 files 沒 OCR 過的 → OCR → 立即 UPDATE + 立即 RENAME/MOVE → 立即 commit\n- 移除 apply mode (併入 main flow)\n- 加 image preprocessing (灰階 + 對比 + 銳化)\n- Filename 規則: memory/2026-08-28-filename-spec.md\n- Folder 結構: <county>/<level>/<grade>/<subject>/\n\n作者: 夥計 (William 指示) - 2026-08-26\n'
import argparse
import csv
import os
import shutil
import sqlite3
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from ocr_extract_metadata import ocr_cover_robust, parse_ocr_text, ARCHIVE_ROOT, DB_PATH
LOG_PATH = Path('/tmp/ocr_streaming.log')
QUARANTINE_CSV = Path('/tmp/ocr_streaming_quarantine.csv')

def log(msg):
    line = f'[{datetime.now().isoformat()}] {msg}'
    print(line, flush=True)
    with LOG_PATH.open('a') as f:
        f.write(line + '\n')
COUNTY_UPGRADE = {'臺北縣': '臺北市', '台北縣': '臺北市', '桃園縣': '桃園市', '臺中縣': '臺中市', '台中縣': '臺中市', '臺南縣': '臺南市', '台南縣': '臺南市', '高雄縣': '高雄市'}

def normalize_county(county: str) -> str:
    """縣市升格: 臺北縣 → 臺北市 等"""
    if not county:
        return county
    return COUNTY_UPGRADE.get(county, county)

def normalize_school_name(school_name: str, county: str) -> str:
    """去 county+縣/市立/國立 prefix. SPEC 8/29 增 '國立'.

    例:
    - '臺南市立復興國中' → '復興國中'
    - '彰化縣立溪湖高中' → '溪湖高中'
    - '國立溪湖高級中學' → '溪湖高級中學'
    """
    if not school_name:
        return school_name
    # 1. 移除 '國立' prefix (常見, 不依 county)
    if school_name.startswith('國立'):
        school_name = school_name[2:]
    # 2. 移除 county+立 prefix
    if not county:
        return school_name
    county_no_suffix = county.replace('市', '').replace('縣', '')
    prefixes = [county + '立', county_no_suffix + '縣立', county_no_suffix + '市立', county + '國立']
    for p in prefixes:
        if school_name.startswith(p):
            return school_name[len(p):]
    return school_name

def digit_to_chinese_year(n: int) -> str:
    """年份數字→中文: 1→一, 2→二, ..., 10→十 (用於段考「第 1 次」→「第一次」)"""
    cn = {1: '一', 2: '二', 3: '三', 4: '四', 5: '五', 6: '六', 7: '七', 8: '八', 9: '九', 10: '十'}
    return cn.get(n, str(n))

def digit_to_chinese_year_only(n: str) -> str:
    """年份數字 (字串) → 中文: '110'→ '一一零' (用於學年)"""
    cn = {'1': '一', '2': '二', '3': '三', '4': '四', '5': '五', '6': '六', '7': '七', '8': '八', '9': '九', '0': '零'}
    return ''.join((cn.get(c, c) for c in str(n)))

def normalize_year(year: str) -> str:
    """學年 normaliz: 統一 3-digit, '一一零' (中文) 也可"""
    if not year:
        return ''
    if year.isdigit() and len(year) == 3:
        return year
    cn_to_digit = {'一': '1', '二': '2', '三': '3', '四': '4', '五': '5', '六': '6', '七': '7', '八': '8', '九': '9', '零': '0'}
    if all((c in cn_to_digit for c in year)):
        return ''.join((cn_to_digit[c] for c in year))
    return year

def init_audit_table(conn):
    cur = conn.cursor()
    cur.execute('\n        CREATE TABLE IF NOT EXISTS ocr_status (\n            paper_id        TEXT PRIMARY KEY,\n            ocr_status      TEXT NOT NULL,\n            county          TEXT,\n            school_name     TEXT,\n            school_year     TEXT,\n            school_term     TEXT,\n            exam_type       TEXT,\n            grade           TEXT,\n            subject         TEXT,\n            ocr_text        TEXT,\n            action_taken    TEXT,\n            error_msg       TEXT,\n            created_at      TEXT DEFAULT CURRENT_TIMESTAMP,\n            updated_at      TEXT DEFAULT CURRENT_TIMESTAMP\n        )\n    ')
    conn.commit()

def fetch_unprocessed(conn, limit=None):
    cur = conn.cursor()
    cur.execute('\n        SELECT f.paper_id, f.rel_path, f.filename, f.county, f.school_name, f.paper_or_daan\n        FROM files f\n        LEFT JOIN ocr_status s ON f.paper_id = s.paper_id\n        WHERE s.paper_id IS NULL\n        ORDER BY f.paper_id\n    ')
    rows = cur.fetchall()
    if limit:
        rows = rows[:limit]
    return [dict(r) for r in rows]

def minimal_parse_from_filename(filename):
    """OCR 失敗時 fallback: 從 filename 拿最少 metadata."""
    if not filename or not filename.endswith('.pdf'):
        return {}
    parts = filename[:-4].split('_')
    if len(parts) < 7:
        return {}
    result = {'county': parts[0] if parts[0] != '未註明' else '', 'subject': parts[3] if len(parts) > 3 and parts[3] != '未註明' else '未分類', 'grade': parts[4] if len(parts) > 4 and parts[4] != '未註明' else '未註明', 'school_name': parts[5] if len(parts) > 5 and parts[5] != '未註明' else ''}
    yr = parts[1] if len(parts) > 1 else '未註明'
    if yr != '未註明':
        m = re.match('^(\\d{3})(上學期|下學期)$', yr)
        if m:
            result['school_year'] = m.group(1)
            result['school_term'] = m.group(2)
        elif re.match('^\\d{3}$', yr):
            result['school_year'] = yr
    if len(parts) > 2 and parts[2] != '未註明':
        result['exam_type'] = parts[2]
    return result

def abs_from_rel(rel, allow_drivefolder_prefix=True, fallback_filename=None):
    """從 rel_path 找 abs_path. fallback 機制:
    1. 直接 path
    2. raw DriveFolder path (_未分類/DriveFolder prefix)
    3. 加 .pdf 或 glob
    4. 用 fallback_filename (檔名部分) 在 ARCHIVE_ROOT 下 glob search
    """
    if not rel:
        return None
    candidates = [ARCHIVE_ROOT / rel]
    if allow_drivefolder_prefix and '_未分類' not in rel and ('_drivefolder' in rel):
        candidates.append(ARCHIVE_ROOT / '_未分類/DriveFolder' / rel)
    for p in candidates:
        if p.is_file():
            return p
    for p in candidates:
        if not str(p).endswith('.pdf') and (not p.is_file()):
            candidate_with_pdf = p.parent / (p.name + '.pdf')
            if candidate_with_pdf.is_file():
                return candidate_with_pdf
            try:
                pdf_candidates = list(p.parent.glob(p.name + '*.pdf'))
                if pdf_candidates:
                    return pdf_candidates[0]
            except (OSError, PermissionError):
                pass
    if fallback_filename:
        try:
            for p in ARCHIVE_ROOT.rglob(fallback_filename):
                if p.is_file():
                    return p
        except (OSError, PermissionError):
            pass
    return None

def level_from_grade(grade, exam_type=None):
    """從 grade 推 level. SPEC 8/29: 國小只有期中考/期末考, 看到「段考/階段/評量」→ 國中/高中, default 國中.

    例: grade=一年級 + exam_type=第二次段考 → 國小一年級不會有段考 → 國中/高中 → default 國中
    """
    if grade in ('一年級', '二年級', '三年級', '四年級', '五年級', '六年級'):
        if exam_type and any((kw in exam_type for kw in ['段考', '階段', '評量'])):
            return '國中'
        return '國小'
    if grade in ('七年級', '八年級', '九年級'):
        return '國中'
    if grade in ('十年級', '十一年級', '十二年級'):
        return '高中'
    return '國中'

def normalize_grade_by_level(grade, level):
    """SPEC 8/29: 國中一年級 → 七年級, 高中一年級 → 十年級.

    因為國中/高中 cover 有時寫「一年級」(口語), 對應官方年級.
    """
    if level == '國中' and grade == '一年級':
        return '七年級'
    if level == '高中' and grade == '一年級':
        return '十年級'
    if level == '國中' and grade == '二年級':
        return '八年級'
    if level == '高中' and grade == '二年級':
        return '十一年級'
    if level == '國中' and grade == '三年級':
        return '九年級'
    if level == '高中' and grade == '三年級':
        return '十二年級'
    return grade

def build_new_filename(parsed, filetype=None):
    """依 SPEC 8/28 + 8/29 建 filename.

    結構: <county>_<school>_<grade>_<year>_<term>_<exam>_<subject>_<version>[_daan].pdf
    - paper: 直接 .pdf (沒 _paper suffix)
    - daan: 加 _daan 結尾

    空/未註明 segment 跳過.
    """
    county = parsed.get('county', '') or ''
    school = parsed.get('school_name', '') or ''
    grade = parsed.get('grade', '') or ''
    year = parsed.get('school_year', '') or ''
    term = parsed.get('school_term', '') or ''
    exam = parsed.get('exam_type', '') or ''
    subject = parsed.get('subject', '') or ''
    version = parsed.get('version', '') or ''
    ft = filetype or parsed.get('filetype', 'paper')
    school = re.sub('_(?:local_[a-z0-9_]+|unknown_[^_]+)_', '_', school)
    school = school.lstrip('_').rstrip('_')
    if county:
        while school.startswith(county):
            school = school[len(county):]
    safe_school = school.replace('/', '／').replace(':', '：')
    safe_exam = exam.replace('/', '／').replace(':', '：')
    safe_subject = subject.replace('/', '／').replace(':', '：')
    safe_grade = grade.replace('/', '／').replace(':', '：')
    parts = []
    for p in [county, safe_school, safe_grade, year, term, safe_exam, safe_subject, version]:
        if p and p != '未註明' and (p != '未分類'):
            parts.append(p)
    if ft == 'daan':
        parts.append('daan')
    if not parts:
        return '_未分類.pdf'
    return '_'.join(parts) + '.pdf'

def build_new_relpath(parsed, filetype=None):
    """建新 rel_path. 依 SPEC 8/28+8/29.

    - county 缺 → _未分類/<grade>/<subject>/
    - county 有 + school 有 → <county>/<school>/<grade>/<subject>/ (school 知名時, 簡化沒 level)
    - county 有 + school 缺 (level 已知) → <county>/<level>/<grade>/ (沒 subject, 簡化)
    - county 有 + school 缺 + level 未明 → <county>/未分類<level>/<grade>/<subject>/
    """
    county = parsed.get('county', '')
    grade = parsed.get('grade', '未註明')
    subject = parsed.get('subject', '未分類')
    school = parsed.get('school_name', '')
    level = level_from_grade(grade, exam_type=parsed.get('exam_type', '')) or '國中'
    ft = filetype or parsed.get('filetype', 'paper')
    if not county:
        parts = ['_未分類']
        if grade and grade != '未註明':
            parts.append(grade)
        if subject and subject != '未分類':
            parts.append(subject)
        return '/'.join(parts)
    if school:
        parts = [county, school, grade, subject]
        parts = [p for p in parts if p and p != '未註明' and (p != '未分類')]
        return '/'.join(parts)
    parts = [county, level, grade]
    parts = [p for p in parts if p and p != '未註明' and (p != '未分類')]
    return '/'.join(parts)

def worker_process_one(record):
    paper_id = record['paper_id']
    rel_path = record['rel_path']
    abs_path = abs_from_rel(rel_path, fallback_filename=record.get('filename'))
    if not abs_path:
        return {'paper_id': paper_id, 'status': 'failed', 'error': 'abs_path not found', 'parsed': None, 'text': ''}
    try:
        text, err = ocr_cover_robust(abs_path)
        if err:
            return {'paper_id': paper_id, 'status': 'failed', 'error': err, 'parsed': None, 'text': ''}
        if not text:
            return {'paper_id': paper_id, 'status': 'failed', 'error': 'ocr_text empty', 'parsed': None, 'text': ''}
        parsed = parse_ocr_text(text)
        if not parsed:
            return {'paper_id': paper_id, 'status': 'failed', 'error': 'parse empty', 'parsed': None, 'text': text[:200]}
        return {'paper_id': paper_id, 'status': 'done', 'parsed': parsed, 'text': text[:200], 'error': None}
    except Exception as e:
        return {'paper_id': paper_id, 'status': 'failed', 'error': str(e), 'parsed': None, 'text': ''}

def update_files_db(conn, paper_id, parsed):
    cur = conn.cursor()
    cur.execute("\n        UPDATE files SET\n            county = COALESCE(NULLIF(?, ''), county),\n            school_name = COALESCE(NULLIF(?, ''), school_name),\n            school_year = COALESCE(NULLIF(?, ''), school_year),\n            school_term = COALESCE(NULLIF(?, ''), school_term),\n            exam_type = COALESCE(NULLIF(?, ''), exam_type),\n            grade = COALESCE(NULLIF(?, ''), grade),\n            subject = COALESCE(NULLIF(?, ''), subject),\n            level = COALESCE(NULLIF(?, ''), level)\n        WHERE paper_id = ?\n    ", (parsed.get('county', ''), parsed.get('school_name', ''), parsed.get('school_year', ''), parsed.get('school_term', ''), parsed.get('exam_type', ''), parsed.get('grade', ''), parsed.get('subject', ''), parsed.get('level', ''), paper_id))
    return cur.rowcount

def record_audit(conn, paper_id, status, parsed, ocr_text, action_taken, error_msg):
    cur = conn.cursor()
    cur.execute('\n        INSERT OR REPLACE INTO ocr_status\n        (paper_id, ocr_status, county, school_name, school_year, school_term,\n         exam_type, grade, subject, ocr_text, action_taken, error_msg, updated_at)\n        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)\n    ', (paper_id, status, parsed.get('county', '') if parsed else None, parsed.get('school_name', '') if parsed else None, parsed.get('school_year', '') if parsed else None, parsed.get('school_term', '') if parsed else None, parsed.get('exam_type', '') if parsed else None, parsed.get('grade', '') if parsed else None, parsed.get('subject', '') if parsed else None, ocr_text[:500] if ocr_text else None, action_taken, error_msg))
    conn.commit()

def main():
    parser = argparse.ArgumentParser(description='Stage 3 Streaming: OCR + UPDATE + RENAME + MOVE 一個 record commit')
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--limit', type=int, default=None)
    parser.add_argument('--dry-run-move', action='store_true', help='dry-run MOVE (只記 log, 不實際 move)')
    parser.add_argument('--rebuild', action='store_true', help='re-OCR records (即使 ocr_status 有紀錄)')
    args = parser.parse_args()
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    init_audit_table(conn)
    if args.rebuild:
        cur = conn.cursor()
        cur.execute('DELETE FROM ocr_status')
        conn.commit()
        log('[rebuild] Cleared ocr_status. Will re-OCR all.')
    unprocessed = fetch_unprocessed(conn, limit=args.limit)
    log(f'[stream] Unprocessed records: {len(unprocessed)}, workers: {args.workers}')
    if not unprocessed:
        log('[stream] No unprocessed records.')
        conn.close()
        return 0
    stats = Counter()
    t0 = time.time()
    processed = 0
    quarantine_rows = []
    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(worker_process_one, r): r for r in unprocessed}
        for f in as_completed(futures):
            record = futures[f]
            paper_id = record['paper_id']
            res = f.result()
            status = res['status']
            parsed = res.get('parsed') or {}
            text = res.get('text') or ''
            error = res.get('error')
            action_taken = 'none'
            rel_path = record['rel_path']
            if status == 'done':
                if parsed.get('county'):
                    parsed['county'] = normalize_county(parsed['county'])
                if parsed.get('school_name') and parsed.get('county'):
                    parsed['school_name'] = normalize_school_name(parsed['school_name'], parsed['county'])
                if parsed.get('school_year'):
                    parsed['school_year'] = normalize_year(parsed['school_year'])
                if not parsed.get('county') and record.get('county'):
                    parsed['county'] = record['county']
                    parsed['county'] = normalize_county(parsed['county'])
                if not parsed.get('school_name') and record.get('school_name'):
                    parsed['school_name'] = record['school_name']
                if not parsed.get('filetype') and record.get('paper_or_daan'):
                    parsed['filetype'] = record['paper_or_daan']
                if parsed.get('school_name') and ('local_' in parsed['school_name'] or 'unknown' in parsed['school_name']):
                    filename = record.get('filename', '')
                    if filename.endswith('_drivefolder.pdf'):
                        fn_parts = filename[:-len('_drivefolder.pdf')].split('_')
                        if len(fn_parts) >= 6:
                            fn_school = fn_parts[5]
                            if fn_school and 'local_' not in fn_school and ('unknown' not in fn_school):
                                parsed['school_name'] = fn_school
                update_files_db(conn, paper_id, parsed)
                file_county, file_school = rel_path.split('/')[:2]
                need_move = parsed.get('county') and parsed.get('county') != file_county or (parsed.get('school_name') and parsed.get('school_name') != file_school)
                if need_move:
                    ft = parsed.get('filetype') or record.get('paper_or_daan') or 'paper'
                    new_rel = build_new_relpath(parsed, filetype=ft)
                    new_filename = build_new_filename(parsed, filetype=ft)
                    new_abs = ARCHIVE_ROOT / new_rel / new_filename
                    old_abs = abs_from_rel(rel_path)
                    if new_abs == old_abs:
                        action_taken = 'no_change'
                    elif new_abs.exists():
                        action_taken = f'skip_target_exists: {new_rel}'
                        quarantine_rows.append({'paper_id': paper_id[:12], 'action': 'move_skip', 'old': rel_path, 'new': new_rel, 'reason': 'target_exists'})
                    elif args.dry_run_move:
                        action_taken = f'dry_run: {rel_path} -> {new_rel}/{new_filename}'
                    else:
                        try:
                            new_abs.parent.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(old_abs), str(new_abs))
                            cur = conn.cursor()
                            cur.execute('UPDATE files SET rel_path = ? WHERE paper_id = ?', (f'{new_rel}/{new_filename}', paper_id))
                            conn.commit()
                            action_taken = f'moved: {rel_path} -> {new_rel}/{new_filename}'
                        except OSError as e:
                            action_taken = f'move_error: {e}'
                else:
                    action_taken = 'db_updated'
            elif status == 'failed':
                minimal = minimal_parse_from_filename(record.get('filename', ''))
                if minimal:
                    minimal['county'] = normalize_county(minimal.get('county', ''))
                    fb_rel = build_new_relpath(minimal)
                    fb_filename = build_new_filename(minimal)
                    fb_abs = ARCHIVE_ROOT / fb_rel / fb_filename
                    if not fb_abs.exists():
                        fb_abs.parent.mkdir(parents=True, exist_ok=True)
                        try:
                            src_abs = abs_from_rel(rel_path)
                            if src_abs and src_abs.is_file():
                                shutil.move(str(src_abs), str(fb_abs))
                                cur = conn.cursor()
                                cur.execute('UPDATE files SET rel_path = ? WHERE paper_id = ?', (f'{fb_rel}/{fb_filename}', paper_id))
                                conn.commit()
                                action_taken = f'fallback_moved: {rel_path} -> {fb_rel}/{fb_filename}'
                            else:
                                action_taken = 'fallback_skip_abs_missing'
                        except OSError as e:
                            action_taken = f'fallback_move_error: {e}'
                    else:
                        action_taken = 'fallback_target_exists'
            record_audit(conn, paper_id, status, parsed, text, action_taken, error)
            stats[status] += 1
            processed += 1
            if processed % 50 == 0:
                elapsed = time.time() - t0
                rate = processed / elapsed if elapsed > 0 else 0
                eta = (len(unprocessed) - processed) / rate if rate > 0 else 0
                log(f'  [{processed}/{len(unprocessed)}] {dict(stats)} {rate:.1f}/s ETA {eta:.0f}s')
    if quarantine_rows:
        with QUARANTINE_CSV.open('a', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=['paper_id', 'action', 'old', 'new', 'reason'])
            if f.tell() == 0:
                writer.writeheader()
            writer.writerows(quarantine_rows)
    elapsed = time.time() - t0
    log(f'[stream] Done {processed} in {elapsed:.1f}s')
    log(f'[stream] Stats: {dict(stats)}')
    log(f'[stream] Log: {LOG_PATH}')
    conn.close()
    return 0
if __name__ == '__main__':
    sys.exit(main())