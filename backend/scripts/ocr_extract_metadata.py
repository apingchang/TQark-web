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

def _preprocess_image(img_input) -> Path:
    """對掃描影像做 preprocess: 灰階 + 對比增強. 提昇 image-based PDF OCR 成功率.

    SPEC 8/29: 接受 Path 或 PIL.Image object (rotation 後直接傳 image 物件).
    """
    from PIL import Image, ImageEnhance, ImageFilter
    try:
        # 1. load image (Path 或 image 物件)
        if isinstance(img_input, Image.Image):
            img = img_input
            # 找 tmp dir 存檔 (傳 image 物件沒有 parent)
            import tempfile
            tmp_dir = Path(tempfile.gettempdir())
            out_path = tmp_dir / f'pre_{id(img)}.png'
        else:
            img = Image.open(img_input)
            out_path = img_input.parent / (img_input.stem + '_pre.png')
        # 灰階
        if img.mode != 'L':
            img = img.convert('L')
        # 對比增強 (×1.5)
        enhancer = ImageEnhance.Contrast(img)
        img = enhancer.enhance(1.5)
        # 銳化
        img = img.filter(ImageFilter.SHARPEN)
        img.save(out_path)
        return out_path
    except Exception:
        return img_input  # 失敗就 return 原輸入


def ocr_cover(pdf_path: Path, lang: str = 'chi_tra+eng', max_pages: int = 3) -> tuple:
    """OCR PDF 前 N 頁 cover (預設 page 1-3), return (text, error).

    SPEC 8/29: 擴大辨識範圍 (Step B)
    - 因為有些 PDF 第 1 頁是題目, metadata 在 page 2-3
    - 先試 pdftotext (vector 文字, 較快較準), 沒字才 OCR image

    改進: pdftotext vector 文字優先 → 沒字才 pdftoppm + tesseract OCR
    """
    # 1. 先試 pdftotext (vector 文字抽取, 包含 metadata 區)
    r = subprocess.run(
        ['pdftotext', '-layout', '-l', str(max_pages), str(pdf_path), '-'],
        capture_output=True, timeout=30
    )
    if r.returncode == 0:
        text = r.stdout.decode('utf-8', errors='ignore').strip()
        # 簡單啟發: 含常見 metadata keyword → 是 metadata 區
        metadata_keywords = ['學年度', '學期', '段考', '考試', '解答', '試題', '年級']
        if text and any(kw in text for kw in metadata_keywords):
            return (text, None)

    # 1b. SPEC 8/31 fix: pdftotext 對直書 layout fail → 試 fitz.get_text() (直書 friendly)
    # 直書 cover 拆字: 每個中文字被分成獨立一行 (e.g. '學\n年\n度')
    # 對直書 text 去換行後再 match metadata keyword
    try:
        import fitz
        with fitz.open(str(pdf_path)) as doc:
            page_count = min(max_pages, len(doc))
            fitz_texts = []
            for i in range(page_count):
                t = doc[i].get_text() or ''
                fitz_texts.append(t)
            fitz_text = '\n'.join(fitz_texts).strip()
            metadata_keywords = ['學年度', '學期', '段考', '考試', '解答', '試題', '年級', '學校']
            fitz_text_compact = fitz_text.replace('\n', '').replace(' ', '')
            if fitz_text and any(kw in fitz_text_compact for kw in metadata_keywords):
                return (fitz_text, None)
    except Exception:
        pass

    # 2. pdftotext 抓不到 → 走 OCR
    # SPEC 8/29 (Step B+): 圖片 PDF 可能需要 rotate 90 度 (順時針) 才能 OCR
    metadata_keywords = ['學年度', '學期', '段考', '考試', '解答', '試題', '年級', '學校', '高級中學', '國中', '國立']
    with tempfile.TemporaryDirectory() as tmpdir:
        prefix = f'{tmpdir}/cover'
        r = subprocess.run(
            ['pdftoppm', '-r', '200', '-f', '1', '-l', str(max_pages), str(pdf_path), prefix],
            capture_output=True, timeout=60
        )
        if r.returncode != 0:
            return ('', 'pdftoppm fail')
        png_files = sorted(list(Path(tmpdir).glob('cover*.ppm')) + list(Path(tmpdir).glob('cover*.png')))
        if not png_files:
            return ('', 'no image produced')

        # 對每頁試 normal + rotated 90 (順時針)
        # 用 PIL 旋轉
        from PIL import Image
        all_text = []
        for png in png_files:
            best_text = ''
            found_metadata = False
            # 試多個 PSM 模式
            for psm in (6, 3):  # 6=uniform block, 3=auto (含直式)
                for rotation in (0, -90):  # 0=normal, -90=順時針
                    try:
                        img = Image.open(png)
                        if rotation == -90:
                            img = img.rotate(rotation, expand=True)
                        preprocessed = _preprocess_image(img)
                        out_prefix = f'{tmpdir}/out_{png.stem}_r{rotation}_p{psm}'
                        r = subprocess.run(
                            ['tesseract', str(preprocessed), out_prefix, '-l', lang, '--psm', str(psm)],
                            capture_output=True, timeout=60
                        )
                        out_txt = Path(out_prefix + '.txt')
                        if r.returncode != 0 or not out_txt.exists():
                            continue
                        page_text = out_txt.read_text()
                        if not best_text or len(page_text) > len(best_text):
                            best_text = page_text
                        # 如果這頁有 metadata keyword → 用這個
                        if any(kw in page_text for kw in metadata_keywords):
                            found_metadata = True
                            break
                    except Exception:
                        continue
                if found_metadata:
                    break
            if best_text:
                all_text.append(best_text)
        if not all_text:
            return ('', 'all pages tesseract fail')
        return ('\n'.join(all_text), None)


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
    (re.compile(r'第\s*([1-3一二三])\s*次\s*階段\s*評量'), lambda m: f'第{m.group(1)}次階段評量'),
    (re.compile(r'第\s*([1-3一二三])\s*階段\s*評量'), lambda m: f'第{m.group(1)}階段評量'),
    (re.compile(r'第\s*([1-3一二三])\s*次\s*定期\s*(?:考|評量)'), lambda m: f'第{m.group(1)}次定期考'),
    (re.compile(r'第\s*([1-3一二三])\s*次\s*段考'), lambda m: f'第{m.group(1)}次段考'),
    (re.compile(r'第\s*([1-3一二三])\s*次\s*模擬考'), lambda m: f'第{m.group(1)}次模擬考'),
    (re.compile(r'第\s*([1-3一二三])\s*次\s*月考'), lambda m: f'第{m.group(1)}次月考'),
    (re.compile(r'期中考'), lambda m: '期中考'),
    (re.compile(r'期末考'), lambda m: '期末考'),
    (re.compile(r'模擬考'), lambda m: '模擬考'),
    (re.compile(r'月考'), lambda m: '月考'),
    (re.compile(r'複習考'), lambda m: '複習考'),
    (re.compile(r'定期(?:評量|考)'), lambda m: '定期評量'),
    (re.compile(r'階段評量'), lambda m: '階段評量'),
]

# 年級
GRADES = [
    '一年級', '二年級', '三年級', '四年級', '五年級', '六年級',
    '七年級', '八年級', '九年級',
    '十年級', '十一年級', '十二年級',
]
# SPEC 8/29: 高中 cover 常寫 '高一/高二/高三' (口語), '高X各班', '高X年級'
GRADE_VARIANTS = {
    '高一': '十年級', '高二': '十一年級', '高三': '十二年級',
    '高一各班': '十年級', '高二各班': '十一年級', '高三各班': '十二年級',
    '高一年級': '十年級', '高二年級': '十一年級', '高三年級': '十二年級',
}

# 學校 patterns: 縣市 + 立/縣立/市立 + 學校名
SCHOOL_PATTERNS = [
    re.compile(r'((?:臺北|台北|新北|基隆|宜蘭|桃園|新竹|苗栗|臺中|台中|彰化|南投|雲林|嘉義|臺南|台南|高雄|屏東|臺東|台東|花蓮|澎湖|金門|連江)[市縣])')
] + [re.compile(c + r"(?:立|縣立|市立)?([^校]{2,15}[國中小高中學])") for c in set(COUNTIES)]


def parse_ocr_text(text: str) -> dict:
    """從 OCR 抽出的 cover page text 解析 metadata. Return dict 或 {}."""
    if not text:
        return {}
    result = {}
    
    # SPEC 8/31 fix: 直書 cover 拆字 (e.g. '學\n年\n度\n第\n二\n學\n期\n')
    # 對直書 text 去換行後再 match metadata keyword, 同時保留原 text 供 record_audit 用
    text_compact = text.replace('\n', '').replace(' ', '') if text else text
    
    # 1. county: 只從 cover header (前 5 行 / 300 chars) 抓, 避免題目內誤判
    # 例: 「臺南市」在 cover 標題 = 真實 county; 「臺南市」在題目/選項 = 干擾
    header_text = text[:300]
    for c in COUNTIES:
        if c in header_text:
            result['county'] = '臺' + c[1:] if c.startswith('台') else c
            break
    # 直書 cover 拆字 case: 用 text_compact 再試 county
    if 'county' not in result:
        for c in COUNTIES:
            if c in text_compact[:300]:
                result['county'] = '臺' + c[1:] if c.startswith('台') else c
                break
    
    # 2. school: 找 XX縣市XX學校 pattern
    # e.g., "桃園市立中興國民中學" / "臺東縣立新生國小"
    school_m = re.search(r'([\u4e00-\u9fff]{2,3}[市縣])立?[\u4e00-\u9fff]{2,15}(?:國[民中小]|高中|高工|商職|家商|工商|高商|高職)', text_compact)
    if not school_m:
        # 直書 cover 拆字: 試原 text
        school_m = re.search(r'([\u4e00-\u9fff]{2,3}[市縣])立?[\u4e00-\u9fff]{2,15}(?:國[民中小]|高中|高工|商職|家商|工商|高商|高職)', text)
    if school_m:
        school_name = school_m.group(0)
    else:
        # 試單純 XX國小/國中/高中 (沒縣市)
        school_m = re.search(r'([\u4e00-\u9fff]{2,15}(?:國民中學|國民小學|國中部|國小部|高中部|高級中學|國中|國小|高中|高工|高商|高職|家商|工商))', text)
        if school_m:
            school_name = school_m.group(0)
        else:
            school_name = ''

    # Bug 1 fix: 移除 placeholder (_local_HASH_, _unknown_xxx_)
    if school_name:
        # 移除 _local_[a-z0-9]+_ 或 _unknown_[^_]+_ 等 placeholder
        # 例: "_新北市_unknown_第一次段考_local_eeac4a47_新北市崇林國中" → "新北市崇林國中"
        # 第一步: 移除 _local_XXX_ 或 _unknown_XXX_
        school_name = re.sub(r'_(?:local_[a-z0-9_]+|unknown_[^_]+)_', '_', school_name)
        # 第二步: 移除多餘的底線
        school_name = re.sub(r'_+', '_', school_name).strip('_')
        result['school_name'] = school_name if school_name else ''
    
    # 3. year: 找 3-digit year + 學年/學年度 (SPEC 8/31: 直書 text_compact 也試)
    year_m = re.search(r'(\d{2,3})\s*學年(?:度)?', text)
    if not year_m:
        year_m = re.search(r'(\d{2,3})\s*學年(?:度)?', text_compact)
    if year_m:
        y = year_m.group(1)
        # Normalize: 2-digit → 3-digit (assume 民國年)
        if len(y) == 2:
            y = '1' + y if int(y) < 50 else '0' + y
        result['school_year'] = y
    
    # 4. term (SPEC 8/31: 直書 text_compact 也試)
    for pat, fn in TERM_PATTERNS:
        m = pat.search(text)
        if m:
            result['school_term'] = fn(m)
            break
    if 'school_term' not in result:
        for pat, fn in TERM_PATTERNS:
            m = pat.search(text_compact)
            if m:
                result['school_term'] = fn(m)
                break
    # Fallback 1: 「(上)不分科系」/「(下)不分科系」 → 上/下學期
    # 也涵蓋「(上)學期」、「(下)學期」 (括號包學期)
    if 'school_term' not in result:
        m = re.search(r'[\(（]\s*(上|下)\s*[\)）]', text)
        if m:
            result['school_term'] = m.group(1) + '學期'
    
    # 5. exam (SPEC 8/31: 直書 text_compact 也試)
    for pat, fn in EXAM_PATTERNS:
        m = pat.search(text)
        if m:
            result['exam_type'] = fn(m)
            break
    if 'exam_type' not in result:
        for pat, fn in EXAM_PATTERNS:
            m = pat.search(text_compact)
            if m:
                result['exam_type'] = fn(m)
                break

    # 5.5 filetype (答案卷 vs 試題卷)
    if any(kw in text for kw in ['答案卷', '標準答案', '解答', '解答卷', '參考答案', '答案紙', '答案  卷', '答案']):
        result['filetype'] = 'daan'
    else:
        result['filetype'] = 'paper'
    
    # 6. grade (年級先抽) — 含 '高X/高X各班/高X年級' 變體 (SPEC 8/31: 直書 text_compact 也試)
    for variant, official in GRADE_VARIANTS.items():
        if variant in text or variant in text_compact:
            result['grade'] = official
            break
    if not result.get('grade'):
        for g in GRADES:
            if g in text or g in text_compact:
                result['grade'] = g
                break

    # 7. level 推算 (先看 school_name 結尾 → fallback grade)
    # 因為「二年級」可能是「國小二年級」或「國中二年級」(8年級), 純 grade 推不夠
    school = result.get('school_name', '') or ''
    if '高級中學' in school or school.endswith('高中部'):
        result['level'] = '高中'
    elif '國民中學' in school or '國中部' in school or school.endswith('國中'):
        result['level'] = '國中'
    elif '國民小學' in school or '國小部' in school or school.endswith('國小'):
        result['level'] = '國小'
    elif result.get('grade'):
        # Fallback: grade 推 (因為 cover 印的「OO 高中」可能錯, 但 grade 是學生實際年級)
        if result['grade'] in ('一年級', '二年級', '三年級', '四年級', '五年級', '六年級'):
            result['level'] = '國小'
        elif result['grade'] in ('七年級', '八年級', '九年級'):
            result['level'] = '國中'
        elif result['grade'] in ('十年級', '十一年級', '十二年級'):
            result['level'] = '高中'

    # 8. subject (longest-match-first + 完整 keywords)
    # SPEC 8/29: 科目一律不加「科」字 — 統一用 '社會', '國文', '英文', '數學', '自然' 等
    # 順序重要: 長的 keyword 先 match (避免「英語」match 到「英語」前面)
    SUBJECTS = [
        # 含「領域/教育」長詞優先
        '自然與生活科技', '健康與體育', '綜合活動領域', '語文學習領域',
        '健康教育', '資訊科技', '生活科技',
        # 加長詞版本 (沒「科」字), 確保 '社會科' 會 match '社會' (不會)
        # 但為了保險, 也放長詞 '自然科學', '社會科學' (不會, 簡寫版本就好)
        # 簡寫 (主體, 無科字)
        '國文', '國語', '英語', '英文', '數學', '自然', '社會', '理化', '生物',
        '歷史', '地理', '公民', '健康', '健體', '體育', '音樂', '美術', '家政',
        '生活', '綜合', '資訊', '科技', '作文', '閱讀', '童軍',
    ]
    # longest first 已經排序 (Python 保持 list 順序)
    # 但 '公民' 和 '社會' 同長度 (2字), '社會' 在 cover 選項/題目常出現 → '社會' 會先 match 而 '公民' 抓不到
    # 修: 把 '公民' 移到 '社會' 之前 (「公民」subject 通常 cover 標題明確寫「公民科」)
    for s in SUBJECTS:
        if s in text:
            # 如果抓到 '社會' 但 cover 標題有 '公民' → 應該是 '公民'
            if s == '社會' and '公民' in text and '社會科' not in text:
                # 確認 '公民' 是更 specific subject, 跳過 '社會'
                continue
            result['subject'] = s
            break

    # SPEC 8/29: 科目一律不加「科」字 — sanitize (e.g., '社會科' → '社會')
    if result.get('subject'):
        result['subject'] = result['subject'].replace('科', '')

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
    
    text, err = ocr_cover_robust(Path(abs_path))
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


# ============================================================
# Stage 5: 直式 OCR 支援
# ============================================================

def ocr_cover_robust(pdf_path: Path, lang: str = 'chi_tra+eng') -> tuple:
    """OCR PDF cover, 支援直式 (rotation fallback). Return (text, error).

    SPEC 8/31 fix: rotation 方向修正 - 直式 paper 需 CW 90° (PIL -90)
    試 normal → CW 90° (-90) → CCW 90° (90) → 180°, 取 metadata 最多的
    """
    text, err = ocr_cover(pdf_path, lang)
    if text and err is None:
        parsed = parse_ocr_text(text)
        # 如果 normal 抓到完整 metadata (school + county) → 直接 return
        if parsed.get('school_name') and parsed.get('county'):
            return (text, None)
        # 否則試 rotation fallback
        candidates = [(text, parsed, 'normal')]
        # 試 CW 90° (-90° in PIL), CCW 90° (90°), 180°
        for angle in (-90, 90, 180):
            text_rot, err_rot = ocr_cover_rotated(pdf_path, lang, angle=angle)
            if text_rot and err_rot is None:
                parsed_rot = parse_ocr_text(text_rot)
                candidates.append((text_rot, parsed_rot, f'rot{angle}'))
        # 取 metadata 最完整的 (school + county + term + exam + grade + subject)
        def score(p):
            return sum(1 for k in ['school_name', 'county', 'school_year', 'school_term', 'exam_type', 'grade', 'subject'] if p.get(k))
        best = max(candidates, key=lambda c: score(c[1]))
        return (best[0], None)
    return (text, err)


def ocr_cover_rotated(pdf_path: Path, lang: str = 'chi_tra+eng', angle: int = 90) -> tuple:
    """OCR PDF cover 但旋轉指定角度. Return (text, error)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        prefix = f'{tmpdir}/cover'
        r = subprocess.run(
            ['pdftoppm', '-r', '150', '-f', '1', '-l', '1', str(pdf_path), prefix],
            capture_output=True, timeout=30
        )
        if r.returncode != 0:
            return ('', 'pdftoppm fail')
        png_files = list(Path(tmpdir).glob('cover*.ppm')) + list(Path(tmpdir).glob('cover*.png'))
        if not png_files:
            return ('', 'no image produced')
        # PIL rotate
        from PIL import Image
        img = Image.open(png_files[0])
        img_rot = img.rotate(angle, expand=True)
        rotated_path = Path(tmpdir) / 'rotated.png'
        img_rot.save(rotated_path)
        out_prefix = f'{tmpdir}/out'
        r = subprocess.run(
            ['tesseract', str(rotated_path), out_prefix, '-l', lang],
            capture_output=True, timeout=60
        )
        out_txt = Path(out_prefix + '.txt')
        if r.returncode != 0 or not out_txt.exists():
            return ('', 'tesseract fail')
        return (out_txt.read_text(), None)


# ============================================================
# Rotate PDF image content (image-based PDF)
# SPEC 8/29: image PDF 旋轉 OCR 後, 必須把 image content rotate 並 save in-place
# ============================================================

def rotate_pdf_content(abs_path, rotation: int = -90):
    """旋轉 PDF 每頁 image content (不是 view rotation). 用 PyMuPDF.

    流程:
    1. PyMuPDF 渲染每頁 → PNG bytes
    2. PIL rotate
    3. 新 PDF 把 rotated image 嵌進去
    4. 覆蓋原檔 (in-place)
    """
    import fitz
    import io
    from PIL import Image
    from pathlib import Path as P

    abs_path = P(abs_path)
    doc = fitz.open(str(abs_path))
    new_doc = fitz.open()
    for page in doc:
        # 渲染 250 DPI
        mat = fitz.Matrix(2.5, 2.5)
        pix = page.get_pixmap(matrix=mat)
        img_bytes = pix.tobytes('png')
        pil_img = Image.open(io.BytesIO(img_bytes))
        pil_rot = pil_img.rotate(rotation, expand=True)
        rot_bytes = io.BytesIO()
        pil_rot.save(rot_bytes, format='PNG')
        rot_bytes.seek(0)
        new_page = new_doc.new_page(width=pil_rot.size[0], height=pil_rot.size[1])
        new_page.insert_image(new_page.rect, stream=rot_bytes.read())
    new_doc.save(str(abs_path))
    new_doc.close()
    doc.close()


def detect_pdf_rotation(abs_path, lang: str = 'chi_tra+eng') -> int:
    """判斷 PDF 是否需要旋轉才能 OCR.

    Return:
    - 0: 不需要
    - -90: 順時針 90°
    - 90: 逆時針 90°
    - 180: 180°
    """
    import fitz
    import io
    import tempfile
    import subprocess
    from PIL import Image
    from pathlib import Path as P

    abs_path = P(abs_path)
    # 渲染 page 1
    doc = fitz.open(str(abs_path))
    if len(doc) == 0:
        return 0
    page = doc[0]
    mat = fitz.Matrix(2, 2)
    pix = page.get_pixmap(matrix=mat)
    img_bytes = pix.tobytes('png')
    doc.close()

    # 試 normal + rotate 90°
    pil_img = Image.open(io.BytesIO(img_bytes))
    scores = {}
    for r in (0, -90, 90, 180):
        if r == 0:
            test_img = pil_img
        else:
            test_img = pil_img.rotate(r, expand=True)
        # save tmp + tesseract
        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
            test_img.save(tmp.name)
            tmp_path = tmp.name
        out_prefix = tmp_path + '_out'
        result = subprocess.run(
            ['tesseract', tmp_path, out_prefix, '-l', lang, '--psm', '3'],
            capture_output=True, timeout=60
        )
        out_txt = P(out_prefix + '.txt')
        if out_txt.exists():
            text = out_txt.read_text()
            # 啟發: 含中文 metadata keyword 越多, 分數越高
            keywords = ['學年度', '學期', '段考', '考試', '解答', '試題', '年級', '學校', '高級中學', '國中', '國立', '科目', '老師', '班']
            score = sum(1 for kw in keywords if kw in text)
            # 加分: 中文字符密度 (中文 OCR 通常 char count > 10 為佳)
            score += min(len([c for c in text if '\u4e00' <= c <= '\u9fff']) / 10, 5)
            scores[r] = score
            P(tmp_path).unlink()
            out_txt.unlink()
        else:
            scores[r] = 0
    # 選最高分 rotation
    best = max(scores, key=scores.get)
    if scores[best] > 0 and best != 0:
        return best
    return 0
