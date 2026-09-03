"""Inbox OCR scan v2: 處理 _inbox/辨識不出/<county>/<year>/<file>.pdf

Strategy:
- 真 PDF (~16,000 files): filename parser + meta.json 補 + OCR cover 補
  → 組 SPEC filename → Move 到 <county>/<level>/<grade>/<subject>/ → INSERT records
- DOC misnamed (.pdf magic != %PDF): rename .pdf → .doc, move 到 <county>/_真word檔_待人工/
- DOCX misnamed: rename .pdf → .docx, move 同上
- _真word檔_待人工/*: skip (本來就 .docx)
- _pending/*: skip
- 其他X/: county = '_未分類'

Filename parsing:
- Type A (中文完整): `[county]<school>[year]學年度第[term]學期[grade]年級[subject]科[exam][type]`
- Type B (簡短): `[year][term][exam][grade][subject]` (e.g., 105上一段國一作文)

Meta.json fallback:
- parsed_subject, parsed_school
"""
import sys, os, re, shutil, sqlite3, hashlib, time, json
from pathlib import Path
from datetime import datetime

sys.path.insert(0, '/home/aping/MyProjects/TQark-web/backend/scripts')
import importlib
o = importlib.import_module('ocr_full_pipeline')
importlib.reload(o)
from ocr_full_pipeline import (
    ARCHIVE_ROOT, DB_PATH,
    build_new_filename, build_new_relpath,
    normalize_county, normalize_school_name, normalize_year,
)
from ocr_extract_metadata import ocr_cover_robust, parse_ocr_text

ARCHIVE = ARCHIVE_ROOT
INBOX_ROOT = Path('/mnt/my_book/考題收集/_inbox')
INBOX_BBQ = ARCHIVE_ROOT / "_inbox" / "辨識不出"
LOG_PATH = '/home/aping/.openclaw/workspace/.openclaw/tmp/inbox_ocr_scan_v2.log'

_log_f = open(LOG_PATH, 'a', buffering=1)


def plog(*args):
    msg = ' '.join(str(a) for a in args)
    _log_f.write(msg + '\n')
    _log_f.flush()
    try:
        print(msg, flush=True)
    except Exception:
        pass


# Grade mapping: 國X → X年級
GRADE_MAP = {
    '國一': '七年級', '國二': '八年級', '國三': '九年級',
    '國四': '十年級', '國五': '十一年級', '國六': '十二年級',
    '小一': '一年級', '小二': '二年級', '小三': '三年級',
    '小四': '四年級', '小五': '五年級', '小六': '六年級',
    '一年': '一年級', '二年': '二年級', '三年': '三年級',
    '四年': '四年級', '五年': '五年級', '六年': '六年級',
    '七年': '七年級', '八年': '八年級', '九年': '九年級',
    '十年': '十年級', '十一年': '十一年級', '十二年': '十二年級',
    '高一': '十年級', '高二': '十一年級', '高三': '十二年級',
}

GRADES_VALID = list(GRADE_MAP.values()) + ['未註明']

# Subject aliases
SUBJECT_ALIAS = {
    '國語文': '國文', '英語': '英文',
    '國語': '國文',
}

SUBJECTS_VALID = ['國文', '國語', '英文', '英語', '數學', '自然', '社會',
                  '理化', '生物', '歷史', '地理', '公民', '健康', '體育',
                  '音樂', '美術', '家政', '生活', '綜合', '資訊', '科技', '作文', '閱讀']

# Exam patterns
EXAM_PATTERNS = [
    ('第一次段考', [r'第一次段考', r'第一次段', r'一段', r'第1次段考', r'第1次段']),
    ('第二次段考', [r'第二次段考', r'第二次段', r'二段', r'第2次段考', r'第2次段']),
    ('第三次段考', [r'第三次段考', r'第三次段', r'三段', r'第3次段考', r'第3次段']),
    ('第一次定期考', [r'第一次定期考', r'第一次定期', r'一定', r'第1次定期考']),
    ('第二次定期考', [r'第二次定期考', r'第二次定期', r'二定', r'第2次定期考']),
    ('第三次定期考', [r'第三次定期考', r'第三次定期', r'三定', r'第3次定期考']),
    ('第一次月考', [r'第一次月考', r'第一次月', r'一月', r'第1次月考']),
    ('第二次月考', [r'第二次月考', r'第二次月', r'二月', r'第2次月考']),
    ('第三次月考', [r'第三次月考', r'第三次月', r'三月', r'第3次月考']),
    ('期中考', [r'期中考', r'期中']),
    ('期末考', [r'期末考', r'期末']),
    ('第一次評量', [r'第一次評量', r'第一次評', r'一評', r'第1次評量']),
    ('第二次評量', [r'第二次評量', r'第二次評', r'二評', r'第2次評量']),
    ('第三次評量', [r'第三次評量', r'第三次評', r'三評', r'第3次評量']),
    ('階段評量', [r'階段評量', r'第1階段', r'第2階段', r'第3階段', r'第一階段', r'第二階段', r'第三階段']),
]


def detect_file_type(abs_p):
    """Detect real file type by magic bytes."""
    try:
        with open(abs_p, 'rb') as f:
            head = f.read(8)
        if head.startswith(b'%PDF'):
            return 'PDF'
        elif head[:4] == b'\xd0\xcf\x11\xe0':
            return 'DOC'
        elif head[:4] == b'PK\x03\x04':
            return 'DOCX'
        elif len(head) == 0:
            return 'EMPTY'
        else:
            return f'OTHER'
    except Exception:
        return 'READ_ERR'


def normalize_subject(s):
    if not s:
        return ''
    s = s.strip()
    return SUBJECT_ALIAS.get(s, s)


def extract_grade_from_filename(filename):
    """Extract grade from filename (any pattern)."""
    # 國X / 高X / 小X first (e.g., 國一, 高一, 小一)
    for abbr, full in GRADE_MAP.items():
        if abbr in filename:
            return full
    
    # X國/社/自/英/數/理: digit/Chinese + subject abbr (e.g., 7國, 8社)
    cn_to_digit = {'一': '1', '二': '2', '三': '3', '四': '4', '五': '5', '六': '6',
                   '七': '7', '八': '8', '九': '9'}
    digit_to_chinese = {'1': '一', '2': '二', '3': '三', '4': '四', '5': '五', '6': '六',
                        '7': '七', '8': '八', '9': '九'}
    m = re.search(r'([一二三四五六七八九])[國社自英數理]', filename)
    if m:
        cn = m.group(1)
        if cn in cn_to_digit:
            return digit_to_chinese[cn_to_digit[cn]] + '年級'
    m = re.search(r'(\d)[國社自英數理]', filename)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 9:
            return digit_to_chinese[str(n)] + '年級'
    
    # X年級
    m = re.search(r'([一二三四五六七八九])年級', filename)
    if m:
        digit_map = {'一': '1', '二': '2', '三': '3', '四': '4', '五': '5', '六': '6',
                     '七': '7', '八': '8', '九': '9'}
        cn = m.group(1)
        if cn in digit_map:
            return digit_map[cn] + '年級'
    m = re.search(r'([1-9])年級', filename)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 9:
            digit_to_chinese = {'1': '一', '2': '二', '3': '三', '4': '四', '5': '五', '6': '六', '7': '七', '8': '八', '9': '九'}
            return digit_to_chinese[str(n)] + '年級'
        elif 10 <= n <= 12:
            return f'{n}年級'
    
    # 純七/八/九 (in filenames like 1131七解答2.pdf)
    digit_map = {'七': '7', '八': '8', '九': '9'}
    for cn, dg_full in digit_map.items():
        if re.search(cn + "(?!年)", filename):
            return cn + "年級"
    
    # X年 (digit) - avoid matching things like 2026年
    m = re.search(r'(\d+)年(?!級)', filename)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 9:
            digit_to_chinese = {'1': '一', '2': '二', '3': '三', '4': '四', '5': '五', '6': '六', '7': '七', '8': '八', '9': '九'}
            return digit_to_chinese[str(n)] + '年級'
        elif 10 <= n <= 12:
            return f'{n}年級'
    
    return 


def extract_year_from_filename(filename):
    """Extract 民國 year from filename."""
    # 學年度 pattern
    m = re.search(r'(1\d{2})學年度', filename)
    if m:
        return m.group(1)
    # [NUM]上/下/1段/2段/3段
    m = re.search(r'(1\d{2})[上下123](?:段|學期|期中考|期末|考|月)', filename)
    if m:
        return m.group(1)
    # 4-digit at start (e.g., 1131七解答)
    m = re.match(r'^(1\d{2})(\d)', filename)
    if m:
        return m.group(1)
    # Standalone 3-digit
    m = re.match(r'^(1\d{2})[_\-]', filename)
    if m:
        return m.group(1)
    # Last fallback
    m = re.search(r'(1\d{2})', filename)
    if m:
        return m.group(1)
    return 


def extract_term_from_filename(filename):
    """Extract term (上/下學期) from filename."""
    if '第一學期' in filename or '上學期' in filename or '上學' in filename or '上1學期' in filename:
        return '上學期'
    if '第二學期' in filename or '下學期' in filename or '下學' in filename or '上2學期' in filename or '下1學期' in filename:
        return '下學期'
    if re.search(r'第1學期', filename):
        return '上學期'
    if re.search(r'第2學期', filename):
        return '下學期'
    if re.search(r'\d{3}上', filename) and (re.search(r'\d{3}上[123一二三四五六七八九]段', filename) or re.search(r'\d{3}上[123]?(?:學期|$|_[^0-9])', filename) or re.search(r'\d{3}上(?=[一二三四五六七八九\d]年級)', filename)):
        return '上學期'
    if re.search(r'\d{3}下', filename) and (re.search(r'\d{3}下[123一二三四五六七八九]段', filename) or re.search(r'\d{3}下[123]?(?:學期|$|_[^0-9])', filename) or re.search(r'\d{3}下(?=[一二三四五六七八九\d]年級)', filename)):
        return '下學期'
    if re.search(r'-\d{1}-', filename):
        m = re.search(r'-(\d{1})-(?:\d|$)', filename)
        if m:
            if m.group(1) == '1':
                return '上學期'
            elif m.group(1) == '2':
                return '下學期'
    # 1131七 pattern (4-digit year + term digit)
    m = re.match(r'^(1\d{2})([123])', filename)
    if m:
        if m.group(2) == '1':
            return '上學期'
        elif m.group(2) == '2':
            return '下學期'
    return 


def extract_exam_from_filename(filename):
    """Extract exam type from filename."""
    # 解答N pattern
    m = re.search(r'解答(\d)', filename)
    if m:
        n = m.group(1)
        if n == '1':
            return '第一次段考'
        elif n == '2':
            return '第二次段考'
        elif n == '3':
            return '第三次段考'
    
    for full, patterns in EXAM_PATTERNS:
        for p in patterns:
            if re.search(p, filename):
                return full
    return 


def extract_subject_from_filename(filename):
    """Extract subject from filename."""
    for s in sorted(SUBJECTS_VALID, key=lambda x: -len(x)):
        if s in filename:
            return normalize_subject(s)
    
    # Digit/Chinese + 國/社/自/英/數/理 pattern
    abbr_subjects = {
        '國': '國文', '社': '社會', '自': '自然',
        '英': '英文', '數': '數學', '理': '理化'
    }
    m = re.search(r'[國社自英數理]', filename)
    if m:
        # Find what's before the abbr
        idx = m.start()
        if idx > 0:
            prev = filename[idx-1]
            digit_map = {'一': '1', '二': '2', '三': '3', '四': '4', '五': '5', '六': '6',
                         '七': '7', '八': '8', '九': '9'}
            if prev in digit_map or prev.isdigit():
                return abbr_subjects.get(m.group(0), '')
    
    return 


def parse_filename_advanced(filename):
    """Parse filename for metadata (more aggressive than minimal_parse_from_filename)."""
    result = {}
    name = filename
    if name.endswith('.pdf'):
        name = name[:-4]
    if name.endswith('.doc'):
        name = name[:-4]
    if name.endswith('.docx'):
        name = name[:-4]
    name = re.sub(r'_daan$', '', name)
    name = re.sub(r'_paper$', '', name)

    result['grade'] = extract_grade_from_filename(name)
    result['school_year'] = extract_year_from_filename(name)
    result['school_term'] = extract_term_from_filename(name)
    result['exam_type'] = extract_exam_from_filename(name)
    result['subject'] = extract_subject_from_filename(name)
    return result


def get_level(grade):
    if grade in ('一年級', '二年級', '三年級', '四年級', '五年級', '六年級'):
        return '國小'
    elif grade in ('七年級', '八年級', '九年級'):
        return '國中'
    elif grade in ('十年級', '十一年級', '十二年級'):
        return '高中'
    return ''


def is_daan_filename(filename):
    """Detect daan (答案) from filename."""
    if '_daan' in filename:
        return True
    daan_keywords = ['解答', '答案', '正解', '解答卷', '答案卷', '正解卷']
    base = filename.replace('.pdf', '').replace('.doc', '').replace('.docx', '')
    for kw in daan_keywords:
        if base.endswith(kw) or f'({kw})' in base or f'（{kw}）' in base:
            return True
    return False


def determine_county_from_folder(county_folder_name):
    """Get canonical county from folder name."""
    if county_folder_name == '其他X':
        return '_未分類'
    if '_pending' in county_folder_name:
        return '_未分類'
    if county_folder_name == '未分類':
        return '_未分類'
    return normalize_county(county_folder_name)


def process_one(abs_p, meta_data=None, county=None):
    """Process one file: detect type → handle accordingly.

    Returns (action, new_rel, msg).
    """
    file_type = detect_file_type(abs_p)
    rel_path = abs_p.relative_to(ARCHIVE).as_posix()
    filename = abs_p.name

    if file_type in ('DOC', 'DOCX'):
        # Rename .pdf → .doc/.docx, move to待人工 folder
        new_ext = '.doc' if file_type == 'DOC' else '.docx'
        new_filename = filename[:-4] + new_ext
        if not county:
            parts = abs_p.parts
            for p in parts:
                if p in ['其他X', '未分類'] or normalize_county(p):
                    county = determine_county_from_folder(p)
                    break
        if not county:
            county = '_未分類'
        # Use abs_p.parent.name as year folder
        target_dir = ARCHIVE / "_inbox" / "辨識不出" / "_真word檔_待人工" / county / abs_p.parent.name
        target = target_dir / new_filename
        if target.exists():
            return ('skip_dup', rel_path, 'target exists')
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(abs_p), str(target))
        meta_src = abs_p.parent / (filename + '.meta.json')
        if meta_src.exists():
            meta_dst = target_dir / (new_filename + '.meta.json')
            shutil.move(str(meta_src), str(meta_dst))
        return ('doc_renamed', str(target.relative_to(ARCHIVE)), f'{file_type} → {new_ext}')

    if file_type != 'PDF':
        return ('skip_unsupported', rel_path, f'unsupported file type: {file_type}')

    return process_pdf(abs_p, meta_data, county, rel_path)


def process_pdf(abs_p, meta_data, county, rel_path):
    """Process a real PDF: parse + rename + move + INSERT record."""
    filename = abs_p.name
    parsed = {}

    # 1. From filename
    fn_parsed = parse_filename_advanced(filename)
    parsed.update({k: v for k, v in fn_parsed.items() if v})

    # 2. From meta.json
    if meta_data:
        if meta_data.get('parsed_subject') and not parsed.get('subject'):
            parsed['subject'] = normalize_subject(meta_data['parsed_subject'])
        if meta_data.get('parsed_school') and not parsed.get('school_name'):
            parsed['school_name'] = meta_data['parsed_school']
        if meta_data.get('parsed_grade') and not parsed.get('grade'):
            parsed['grade'] = meta_data['parsed_grade']

    # 3. From src path (county/school/grade/subject)
    if meta_data and meta_data.get('src'):
        src_parts = meta_data['src'].split('/')
        # pattern 1: _inbox/<county>/<level>/<grade>/<subject>/<filetype>/<file>
        # pattern 2: _inbox/<county>_schools/<school>/<year>/<year>-<term>-<exam>/<file>
        if len(src_parts) >= 6 and src_parts[2] in ('國小', '國中', '高中'):
            # pattern 1: extract grade/subject from path
            if not parsed.get('grade'):
                parsed['grade'] = src_parts[3]
            if not parsed.get('subject') and src_parts[4] not in ('daan', 'paper'):
                parsed['subject'] = normalize_subject(src_parts[4])
            # paper_or_daan from path
            if src_parts[5] in ('daan', 'paper'):
                parsed['paper_or_daan_from_path'] = src_parts[5]
        elif len(src_parts) >= 4:
            # pattern 2: extract school from path
            school_folder = src_parts[2]
            if school_folder and not parsed.get('school_name'):
                parsed['school_name'] = school_folder

    # County
    if not county:
        county = '_未分類'
    parsed['county'] = county

    # Normalize school name
    if parsed.get('school_name') and county != '_未分類':
        parsed['school_name'] = normalize_school_name(parsed['school_name'], county)

    # Detect daan (from src path > filename)
    if parsed.get('paper_or_daan_from_path'):
        filetype = parsed['paper_or_daan_from_path']
    else:
        is_daan = is_daan_filename(filename)
        filetype = 'daan' if is_daan else 'paper'
    parsed['paper_or_daan'] = filetype

    # Level
    parsed['level'] = get_level(parsed.get('grade', ''))

    # Subject guard
    if parsed.get('subject') == '社會':
        for sub in ['公民', '地理', '歷史']:
            if sub in filename:
                parsed['subject'] = sub
                break

    # Required check
    required = ['county', 'grade', 'subject', 'school_year']
    missing = [r for r in required if not parsed.get(r)]
    if missing:
        return ('skip_low_quality', rel_path,
                f'missing: {missing}, parsed: {parsed}')

    final = {
        'county': parsed['county'],
        'school_name': parsed.get('school_name', ''),
        'school_year': parsed.get('school_year', ''),
        'school_term': parsed.get('school_term', ''),
        'exam_type': parsed.get('exam_type', ''),
        'grade': parsed['grade'],
        'subject': parsed['subject'],
        'level': parsed.get('level', ''),
        'filetype': filetype,
        'version': '未註明',
    }

    new_filename = build_new_filename(final, filetype=filetype)
    new_rel = build_new_relpath(final, filetype=filetype)
    new_rel_full = f'{new_rel}/{new_filename}'
    new_abs = ARCHIVE / new_rel_full

    if new_abs == abs_p:
        return ('skip_same', rel_path, 'already at target')

    if new_abs.exists():
        return ('skip_dup', rel_path, f'target exists: {new_rel_full}')

    new_abs.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(abs_p), str(new_abs))
    except Exception as e:
        return ('error_move', rel_path, f'move failed: {e}')

    # Move meta.json
    meta_src = abs_p.parent / (filename + '.meta.json')
    if meta_src.exists():
        meta_dst = new_abs.parent / (new_filename + '.meta.json')
        try:
            shutil.move(str(meta_src), str(meta_dst))
        except Exception:
            pass

    # Cleanup empty parents
    parent = abs_p.parent
    while parent != ARCHIVE and parent.exists():
        try:
            parent.rmdir()
            parent = parent.parent
        except OSError:
            break

    # INSERT record
    try:
        with open(new_abs, 'rb') as f:
            content_hash = hashlib.sha256(f.read()).hexdigest()[:12]
        size_kb = new_abs.stat().st_size // 1024
        mtime = datetime.fromtimestamp(new_abs.stat().st_mtime).isoformat()

        if parsed['grade'] in ('一年級', '二年級', '三年級', '四年級', '五年級', '六年級'):
            lvl = '國小'
        elif parsed['grade'] in ('七年級', '八年級', '九年級'):
            lvl = '國中'
        elif parsed['grade'] in ('十年級', '十一年級', '十二年級'):
            lvl = '高中'
        else:
            lvl = ''

        with sqlite3.connect(DB_PATH, timeout=60) as conn:
            cur = conn.cursor()
            cur.execute("""INSERT OR REPLACE INTO files
                (paper_id, rel_path, filename, county, level, school_year, school_term, exam_type, grade, subject, paper_or_daan, school_name, version, size_kb, mtime, is_paper, has_school, has_school_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (content_hash, new_rel_full, new_filename, parsed['county'], lvl or '_未分類',
                 parsed.get('school_year') or None, parsed.get('school_term') or None,
                 parsed.get('exam_type') or None, parsed['grade'] or None,
                 parsed['subject'] or None, filetype,
                 parsed.get('school_name') or None, '未註明',
                 size_kb, mtime,
                 1 if filetype == 'paper' else 0, 1,
                 1 if parsed.get('school_name') else 0))
            conn.commit()
        return ('moved_inserted', new_rel_full,
                f'county={parsed["county"]} school={parsed.get("school_name","")} grade={parsed.get("grade","")} year={parsed.get("school_year","")} term={parsed.get("school_term","")} exam={parsed.get("exam_type","")} subject={parsed.get("subject","")}')
    except Exception as e:
        return ('moved_no_db', new_rel_full, f'INSERT failed: {e}')


def main():
    plog(f'=== inbox_ocr_scan_v2 start: {datetime.now().isoformat()} ===')

    pdf_files = []
    doc_files = []
    skipped_pending = 0

    for entry in (ARCHIVE / "_inbox" / "辨識不出").iterdir():
        if not entry.is_dir():
            continue
        if entry.name == '_真word檔_待人工':
            continue
        if entry.name.endswith('_pending'):
            skipped_pending += 1
            continue
        county = determine_county_from_folder(entry.name)
        for root, dirs, files in os.walk(entry, followlinks=False):
            for f in files:
                if f.endswith('.pdf'):
                    pdf_files.append((Path(root) / f, county))
                elif f.endswith(('.doc', '.docx')):
                    doc_files.append((Path(root) / f, county))

    plog(f'Total PDFs to scan: {len(pdf_files)}')
    plog(f'Total DOC/DOCX (misnamed .pdf): {len(doc_files)}')
    plog(f'Skipped _pending folders: {skipped_pending}')

    stats = {
        'moved_inserted': 0, 'moved_no_db': 0, 'doc_renamed': 0,
        'skip_low_quality': 0, 'skip_dup': 0, 'skip_same': 0,
        'skip_unsupported': 0, 'error_move': 0, 'error': 0,
    }
    errors = []

    t0 = time.time()
    last_log = time.time()
    total = len(pdf_files) + len(doc_files)

    for i, (abs_p, county) in enumerate(pdf_files + doc_files):
        meta_data = None
        meta_path = abs_p.parent / (abs_p.name + '.meta.json')
        if meta_path.exists():
            try:
                with open(meta_path, 'r', encoding='utf-8') as f:
                    meta_data = json.load(f)
            except Exception:
                pass

        try:
            action, new_rel, msg = process_one(abs_p, meta_data, county)
        except Exception as e:
            action = 'error'
            msg = str(e)
            new_rel = abs_p.relative_to(ARCHIVE).as_posix()
            errors.append((new_rel, str(e)))

        stats[action] = stats.get(action, 0) + 1

        now = time.time()
        if (i+1) % 50 == 0 or (now - last_log) > 5:
            plog(f'[{i+1}/{total}] {action}: {msg[:120]}')
            last_log = now

    plog(f'\n=== Final ({(time.time()-t0)/60:.1f} min) ===')
    for k, v in sorted(stats.items()):
        plog(f'  {k}: {v}')
    if errors:
        plog(f'\nErrors ({len(errors)}):')
        for rel, err in errors[:20]:
            plog(f'  {rel[:80]}: {err[:100]}')

    plog(f'=== Done: {datetime.now().isoformat()} ===')


if __name__ == '__main__':
    main()
