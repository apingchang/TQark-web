#!/usr/bin/env python3
"""
Stage 2-3 OCR Pipeline: 從 PDF cover 抽 (county, school, year, term, exam, grade, subject)
用於 quarantine list + 全 records 修正。

執行模式:
- cover: 抽 cover page (page 1) → parse → 寫 quarantine list CSV
- dry-run: 抽 cover → 對比 DB → 寫 quarantine list + ocr_status table
- full: 抽 cover + parse + UPDATE DB + rename + move (background job)

輸出:
- /tmp/ocr_results.csv: paper_id / parsed county/school/year/term/exam/grade/subject / ocr_text / confidence
- /tmp/ocr_quarantine.csv: records 跟 filename/path 不一致

作者: 夥計 (William 指示) - 2026-08-26
"""
import argparse
import csv
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

DB_PATH = '/home/aping/MyProjects/TQark-web/backend/state/tqark-web.db'
ARCHIVE_ROOT = Path('/mnt/my_book/考題收集')

def _abs_from_rel(rel: str) -> str:
    return str(ARCHIVE_ROOT / rel) if rel else ""

# ============================================================
# OCR 抽 cover page text
# ============================================================

def ocr_cover(pdf_path: Path, lang: str = 'chi_tra+eng') -> tuple:
    """OCR PDF 第 1 頁 cover, return (text, error)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        prefix = f'{tmpdir}/cover'
        # 1. PDF → PNG
        r = subprocess.run(
            ['pdftoppm', '-r', '150', '-f', '1', '-l', '1', str(pdf_path), prefix],
            capture_output=True, timeout=30
        )
        if r.returncode != 0:
            return ('', 'pdftoppm fail')
        # 2. 找產生的 PNG/PPM
        png_files = list(Path(tmpdir).glob('cover*.ppm')) + list(Path(tmpdir).glob('cover*.png'))
        if not png_files:
            return ('', 'no image produced')
        # 3. tesseract OCR
        out_prefix = f'{tmpdir}/out'
        r = subprocess.run(
            ['tesseract', str(png_files[0]), out_prefix, '-l', lang],
            capture_output=True, timeout=60
        )
        out_txt = Path(out_prefix + '.txt')
        if r.returncode != 0 or not out_txt.exists():
            return ('', 'tesseract fail')
        return (out_txt.read_text(), None)


# ============================================================
# Parse OCR text → metadata
# ============================================================

# 22 個台灣縣市
COUNTIES = [
    '臺北市', '台北市', '新北市', '基隆市', '宜蘭縣', '桃園市', '新竹市', '新竹縣',
    '苗栗縣', '臺中市', '台中市', '彰化縣', '南投縣', '雲林縣', '嘉義市', '嘉義縣',
    '臺南市', '台南市', '高雄市', '屏東縣', '臺東縣', '台東縣', '花蓮縣', '澎湖縣',
    '金門縣', '連江縣',
]

def _term_digit_to_chinese(m):
    """'1'/'一' → '上學期', '2'/'二' → '下學期', '3'/'三' → '第3學期'(保留)"""
    d = m.group(1)
    return {'1': '上學期', '一': '上學期', '2': '下學期', '二': '下學期'}.get(d, f'第{d}學期')


# 學期 patterns (含第1/2/3/上下/未填等變體)
TERM_PATTERNS = [
    (re.compile(r'第\s*([1-3一二三])\s*學期'), _term_digit_to_chinese),
    (re.compile(r'(上|下)學期'), lambda m: m.group(0)),
    (re.compile(r'(上|下)半\s*年'), lambda m: f'{m.group(1)}學期'),
]

# 段考 patterns
EXAM_PATTERNS = [
    (re.compile(r'第\s*([1-3一二三])\s*次\s*定期\s*(?:考|評量)'), lambda m: f'第{m.group(1)}次定期考'),  # OK 因為 f-string 內 m.group
    (re.compile(r'第\s*([1-3一二三])\s*次\s*段考'), lambda m: f'第{m.group(1)}次段考'),
    (re.compile(r'第\s*([1-3一二三])\s*階段\s*評量'), lambda m: f'第{m.group(1)}階段評量'),
    (re.compile(r'期中考'), lambda m: '期中考'),
    (re.compile(r'期末考'), lambda m: '期末考'),
    (re.compile(r'(?:第\s*[1-3一二三]\s*次)?\s*模擬考'), lambda m: '模擬考'),
    (re.compile(r'月考'), lambda m: '月考'),
    (re.compile(r'複習考'), lambda m: '複習考'),
    (re.compile(r'定期(?:考|評量)'), lambda m: '定期評量'),
]

# 年級
GRADES = ['一年級', '二年級', '三年級', '四年級', '五年級', '六年級', '七年級', '八年級', '九年級', '十年級', '十一年級', '十二年級']

# 學校 patterns: 縣市 + 立/縣立/市立 + 學校名
SCHOOL_PATTERNS = [
    re.compile(r'((?:臺北|台北|新北|基隆|宜蘭|桃園|新竹|苗栗|臺中|台中|彰化|南投|雲林|嘉義|臺南|台南|高雄|屏東|臺東|台東|花蓮|澎湖|金門|連江)[市縣])')
] + [re.compile(c + r"(?:立|縣立|市立)?([^校]{2,15}[國中小高中學])") for c in set(COUNTIES)]


def parse_ocr_text(text: str) -> dict:
    """從 OCR 抽出的 cover page text 解析 metadata. Return dict 或 {}."""
    if not text:
        return {}
    result = {}
    
    # 1. county: 找 COUNTY 出現的位置
    for c in COUNTIES:
        if c in text:
            result['county'] = '臺' + c[1:] if c.startswith('台') else c
            break
    
    # 2. school: 找 XX縣市XX學校 pattern
    # e.g., "桃園市立中興國民中學" / "臺東縣立新生國小"
    school_m = re.search(r'([\u4e00-\u9fff]{2,3}[市縣])立?[\u4e00-\u9fff]{2,15}(?:國[民中小]|高中|高工|商職|家商|工商|高商|高職)', text)
    if school_m:
        result['school_name'] = school_m.group(0)
    else:
        # 試單純 XX國小/國中/高中 (沒縣市)
        school_m = re.search(r'([\u4e00-\u9fff]{2,15}(?:國民中學|國民小學|國中|國小|高中|高工|高商|高職|家商|工商))', text)
        if school_m:
            result['school_name'] = school_m.group(0)
    
    # 3. year: 找 3-digit year + 學年/學年度
    year_m = re.search(r'(\d{2,3})\s*學年(?:度)?', text)
    if year_m:
        y = year_m.group(1)
        # Normalize: 2-digit → 3-digit (assume 民國年)
        if len(y) == 2:
            y = '1' + y if int(y) < 50 else '0' + y
        result['school_year'] = y
    
    # 4. term
    for pat, fn in TERM_PATTERNS:
        m = pat.search(text)
        if m:
            result['school_term'] = fn(m)
            break
    
    # 5. exam
    for pat, fn in EXAM_PATTERNS:
        m = pat.search(text)
        if m:
            result['exam_type'] = fn(m)
            break
    
    # 6. grade
    for g in GRADES:
        if g in text:
            result['grade'] = g
            break
    
    # 7. subject (從常見科目)
    SUBJECTS = ['國文', '國語', '英語', '英文', '數學', '自然', '社會', '理化', '生物',
                '歷史', '地理', '公民', '健康', '健體', '體育', '音樂', '美術', '家政',
                '生活', '綜合活動', '資訊', '科技', '作文', '閱讀']
    for s in SUBJECTS:
        if s in text:
            result['subject'] = s
            break
    
    return result


# ============================================================
# Quarantine logic
# ============================================================

def fetch_records_to_ocr(conn, only_drivefolder=False, only_empty_metadata=False):
    """抓要 OCR 的 records. by default: 全部 records."""
    cur = conn.cursor()
    where = []
    if only_drivefolder:
        where.append("rel_path LIKE '%_drivefolder.pdf'")
    if only_empty_metadata:
        where.append("(school_year = '' OR school_year IS NULL OR school_term = '' OR school_term IS NULL OR exam_type = '' OR exam_type IS NULL OR school_name = '' OR school_name IS NULL OR county = '' OR county IS NULL)")
    where_sql = 'WHERE ' + ' AND '.join(where) if where else ''
    cur.execute(f"""
        SELECT paper_id, rel_path, filename,
            county, school_name, school_year, school_term, exam_type, grade, subject
        FROM files
        {where_sql}
        ORDER BY paper_id
    """)
    return [dict(r) for r in cur.fetchall()]


def quarantine_check(parsed: dict, db_record: dict) -> dict:
    """對比 OCR parsed vs DB record. Return diff dict."""
    diffs = {}
    for k in ['county', 'school_name', 'school_year', 'school_term', 'exam_type', 'grade', 'subject']:
        ocr_val = parsed.get(k, '')
        db_val = db_record.get(k, '') or ''
        if ocr_val and db_val and ocr_val != db_val:
            diffs[k] = {'ocr': ocr_val, 'db': db_val}
    return diffs


# ============================================================
# Worker function for parallel processing
# ============================================================

def ocr_one_record(record: dict, do_ocr=True) -> dict:
    """OCR + parse 單個 record. Used by ProcessPoolExecutor."""
    abs_path = record.get('abs_path') or _abs_from_rel(record.get('rel_path', ''))
    if not abs_path or not Path(abs_path).exists():
        return {'paper_id': record['paper_id'], 'error': 'abs_path missing', 'parsed': {}}
    if not do_ocr:
        return {'paper_id': record['paper_id'], 'parsed': {}, 'text': ''}
    
    text, err = ocr_cover(Path(abs_path))
    if err:
        return {'paper_id': record['paper_id'], 'error': err, 'parsed': {}}
    
    parsed = parse_ocr_text(text)
    return {'paper_id': record['paper_id'], 'parsed': parsed, 'text': text[:500]}


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(description='Stage 2-3 OCR pipeline')
    parser.add_argument('--mode', choices=['cover-only', 'quarantine', 'full'], default='quarantine',
                        help='cover-only: 只 OCR cover page; quarantine: OCR + 對比 DB; full: OCR + UPDATE')
    parser.add_argument('--limit', type=int, default=None, help='limit records (for testing)')
    parser.add_argument('--workers', type=int, default=4, help='parallel workers')
    parser.add_argument('--only-drivefolder', action='store_true', help='only OCR _drivefolder records')
    parser.add_argument('--only-empty-metadata', action='store_true', help='only OCR records with empty metadata')
    parser.add_argument('--output', type=Path, default=Path('/tmp/ocr_results.csv'),
                        help='output CSV path')
    parser.add_argument('--quarantine', type=Path, default=Path('/tmp/ocr_quarantine.csv'),
                        help='quarantine list CSV path')
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # 1. fetch records
    records = list(fetch_records_to_ocr(
        conn,
        only_drivefolder=args.only_drivefolder,
        only_empty_metadata=args.only_empty_metadata,
    ))
    if args.limit:
        records = records[:args.limit]
    print(f'[OCR] Mode: {args.mode}, records: {len(records)}, workers: {args.workers}')

    # 2. OCR in parallel
    t0 = time.time()
    results = []
    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(ocr_one_record, r, True): r for r in records}
        for i, f in enumerate(as_completed(futures)):
            res = f.result()
            results.append(res)
            if (i + 1) % 100 == 0:
                elapsed = time.time() - t0
                rate = (i + 1) / elapsed
                eta = (len(records) - i - 1) / rate if rate > 0 else 0
                print(f'  [{i+1}/{len(records)}] {rate:.1f}/s, ETA {eta:.0f}s')

    elapsed = time.time() - t0
    print(f'[OCR] Done in {elapsed:.1f}s ({len(records)/elapsed:.1f} records/s)')

    # 3. process results
    ocr_csv = args.output.open('w', newline='')
    csv_writer = csv.writer(ocr_csv)
    csv_writer.writerow(['paper_id', 'rel_path', 'field', 'db_val', 'ocr_val', 'ocr_text_preview'])

    quarantine_csv = args.quarantine.open('w', newline='')
    q_writer = csv.writer(quarantine_csv)
    q_writer.writerow(['paper_id', 'rel_path', 'field', 'db_val', 'ocr_val', 'ocr_text_preview'])

    stats = Counter()
    diff_stats = Counter()
    
    for rec, res in zip(records, results):
        paper_id = rec['paper_id']
        rel_path = rec['rel_path']
        parsed = res.get('parsed', {})
        text = res.get('text', '')
        err = res.get('error', '')
        
        if err:
            stats['error'] += 1
            continue
        if not parsed:
            stats['empty_parse'] += 1
            continue
        
        stats['parsed'] += 1
        # 對比 DB
        diffs = quarantine_check(parsed, rec)
        for field, d in diffs.items():
            diff_stats[field] += 1
            csv_writer.writerow([paper_id[:12], rel_path, field, d['db'], d['ocr'], text[:80]])
            # 大差異 (county / school_name) 進 quarantine
            if field in ['county', 'school_name']:
                q_writer.writerow([paper_id[:12], rel_path, field, d['db'], d['ocr'], text[:80]])

    ocr_csv.close()
    quarantine_csv.close()

    print(f'\n[OCR] === stats ===')
    for k, v in stats.items():
        print(f'  {k}: {v}')
    print(f'\n[OCR] === diff stats (DB vs OCR) ===')
    for k, v in diff_stats.most_common():
        print(f'  {k}: {v}')

    print(f'\n[OCR] Results CSV: {args.output}')
    print(f'[OCR] Quarantine CSV: {args.quarantine}')
    conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
