"""Inbox OCR scan: 對 _inbox/ 內 disk files (orphan, 沒 records), 從 folder path + filename 拆 metadata + INSERT records + move file.

Strategy (different from _未分類/):
- _inbox/ 內 folder結構: `<county>_schools/<school>/<grade>/<subject>/<year>?/<exam>?/file.pdf`
- 例 `_inbox/高雄市_schools/獅甲國中/七年級/數學/期末考.pdf` → county=高雄市, school=獅甲國中, grade=七年級, subject=數學, exam=期末考
- 從 folder path 拆 metadata (county/school/grade/subject)
- 從 filename 拆 year/term/exam/version

- _pending subdirs: skip (未進來的)
- 辨識不出 subdir: skip (沒 county)
- 其他X: skip (legacy)
- 高雄市_unclassified: scan (個別 file 沒 folder structure)
"""
import sys, os, re, shutil, sqlite3, hashlib, time
from pathlib import Path
from datetime import datetime

sys.path.insert(0, '/home/aping/MyProjects/TQark-web/backend/scripts')
import importlib
o = importlib.import_module('ocr_full_pipeline')
e = importlib.import_module('ocr_extract_metadata')
importlib.reload(o)
importlib.reload(e)
from ocr_full_pipeline import (
    ARCHIVE_ROOT, DB_PATH,
    build_new_filename, build_new_relpath,
    normalize_county, normalize_school_name, normalize_year,
)

ARCHIVE = ARCHIVE_ROOT
LOG_PATH = '/home/aping/.openclaw/workspace/.openclaw/tmp/inbox_ocr_scan.log'

_log_f = open(LOG_PATH, 'a', buffering=1)


def plog(*args):
    msg = ' '.join(str(a) for a in args)
    _log_f.write(msg + '\n')
    _log_f.flush()
    try:
        print(msg)
    except Exception:
        pass


GRADES_VALID = ['一年級', '二年級', '三年級', '四年級', '五年級', '六年級',
                '七年級', '八年級', '九年級', '十年級', '十一年級', '十二年級']
SUBJECTS_VALID = ['國文', '國語', '英文', '英語', '數學', '自然', '社會',
                 '理化', '生物', '歷史', '地理', '公民', '健康', '體育',
                 '音樂', '美術', '家政', '生活', '綜合', '資訊', '科技', '作文', '閱讀']
EXAM_VALID = ['第一次段考', '第二次段考', '第三次段考',
              '第一次月考', '第二次月考', '第三次月考',
              '第一次定期考', '第二次定期考', '第三次定期考',
              '第一次評量', '第二次評量', '第三次評量',
              '期中考', '期末考', '考試']


def extract_grade_from_text(text):
    text_clean = re.sub(r'[（()].*?[）)]', '', text)
    for g in GRADES_VALID:
        if g in text_clean:
            return g
    return ''


def extract_year_from_text(text):
    m = re.search(r'(\d{3})', text)
    return m.group(1) if m else ''


def extract_subject(text):
    for s in SUBJECTS_VALID:
        if s in text:
            return s
    return ''


def extract_exam_from_text(text):
    for e in EXAM_VALID:
        if e in text:
            return e
    return ''


def parse_inbox_path(rel_path):
    parts = rel_path.split('/')
    if len(parts) < 2:
        return {}
    top = parts[1]

    skip_tops = {'其他X', '辨識不出', '_未分類', '未分類'}
    if top in skip_tops:
        return {}

    if top.endswith('_pending'):
        return {}

    if top.endswith('_schools'):
        county = top[:-len('_schools')]
        county = normalize_county(county)
        if not county:
            return {}

        if len(parts) < 6:
            return {}

        school = parts[2]
        grade_text = parts[3]
        subject_text = parts[4]

        grade = extract_grade_from_text(grade_text)
        subject = extract_subject(subject_text)

        filename = parts[-1]
        year = extract_year_from_text(grade_text)
        if not year:
            year = extract_year_from_text(filename)
        exam = extract_exam_from_text(filename)
        version_match = re.search(r'_(康軒|翰林|南一|何嘉仁|仁林|育橋)(?:_daan)?\.pdf$', filename)
        version = version_match.group(1) if version_match else '未註明'

        is_daan = '_daan' in filename
        filetype = 'daan' if is_daan else 'paper'

        if '上學期' in filename:
            term = '上學期'
        elif '下學期' in filename:
            term = '下學期'
        else:
            term = ''

        if not grade or not subject:
            return {}

        return {
            'county': county, 'school': school, 'grade': grade,
            'subject': subject, 'year': year, 'term': term,
            'exam': exam or '', 'version': version, 'filetype': filetype,
        }

    return {}


def main():
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    folder = Path('/mnt/my_book/考題收集/_inbox')

    pdf_files = []
    for entry in os.scandir(folder):
        if not entry.is_dir():
            continue
        if entry.name.endswith('_pending'):
            continue
        if entry.name in ('其他X', '辨識不出'):
            continue
        for root, dirs, files in os.walk(entry.path, followlinks=False):
            for f in files:
                if f.endswith('.pdf'):
                    pdf_files.append(Path(root) / f)

    plog(f'Total _inbox pdf files (skip _pending, 辨識不出, 其他X): {len(pdf_files)}')

    stats = {'moved': 0, 'updated': 0, 'errors': 0, 'skip': 0, 'no_metadata': 0}
    t0 = time.time()

    for i, abs_p in enumerate(pdf_files):
        rel_path = abs_p.relative_to(ARCHIVE).as_posix()
        filename = abs_p.name

        meta = parse_inbox_path(rel_path)
        if not meta:
            stats['no_metadata'] += 1
            if i < 20:
                plog(f'[{i+1}] [NO_META] {rel_path[:80]}')
            continue

        final = {
            'county': meta['county'], 'school_name': meta['school'],
            'school_year': meta['year'], 'school_term': meta['term'],
            'exam_type': meta['exam'], 'grade': meta['grade'],
            'subject': meta['subject'], 'level': '',
            'filetype': meta['filetype'], 'version': meta['version'],
        }

        new_rel = build_new_relpath(final, filetype=meta['filetype'])
        new_filename = build_new_filename(final, filetype=meta['filetype'])
        new_rel_full = f'{new_rel}/{new_filename}'
        new_abs = ARCHIVE / new_rel_full

        if (i+1) % 100 == 0 or i < 5:
            plog(f'[{i+1}/{len(pdf_files)}] {rel_path[:80]}')
            plog(f'  → {new_rel_full}')

        if new_abs == abs_p:
            stats['skip'] += 1
        else:
            try:
                new_abs.parent.mkdir(parents=True, exist_ok=True)
                if new_abs.is_file():
                    stats['skip'] += 1
                    continue
                shutil.move(str(abs_p), str(new_abs))
                parent = abs_p.parent
                while parent != ARCHIVE and parent.is_dir():
                    try:
                        parent.rmdir()
                        parent = parent.parent
                    except OSError:
                        break
                stats['moved'] += 1
            except Exception as e:
                stats['errors'] += 1
                continue

        try:
            with open(new_abs, 'rb') as f:
                content_hash = hashlib.sha256(f.read()).hexdigest()[:12]
            size_kb = new_abs.stat().st_size // 1024
            mtime = datetime.fromtimestamp(new_abs.stat().st_mtime).isoformat()

            if meta['grade'] in ('一年級', '二年級', '三年級', '四年級', '五年級', '六年級'):
                lvl = '國小'
            elif meta['grade'] in ('七年級', '八年級', '九年級'):
                lvl = '國中'
            elif meta['grade'] in ('十年級', '十一年級', '十二年級'):
                lvl = '高中'
            else:
                lvl = ''

            cur.execute("""INSERT OR REPLACE INTO files
                (paper_id, rel_path, filename, county, level, school_year, school_term, exam_type, grade, subject, paper_or_daan, school_name, version, size_kb, mtime, is_paper, has_school, has_school_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (content_hash, new_rel_full, new_filename, meta['county'], lvl or '_未分類',
                 meta['year'] or None, meta['term'] or None, meta['exam'] or None,
                 meta['grade'] or None, meta['subject'] or None, meta['filetype'], meta['school'] or None,
                 meta['version'], size_kb, mtime,
                 1 if meta['filetype'] == 'paper' else 0, 1, 1))
            conn.commit()
            stats['updated'] += 1
        except Exception as e:
            stats['errors'] += 1
            continue

    conn.close()
    plog(f'\n=== Final ===')
    plog(f'Moved: {stats["moved"]}')
    plog(f'Updated: {stats["updated"]}')
    plog(f'Skip: {stats["skip"]}')
    plog(f'No metadata: {stats["no_metadata"]}')
    plog(f'Errors: {stats["errors"]}')
    plog(f'Total time: {time.time()-t0:.0f}s')


if __name__ == '__main__':
    main()
