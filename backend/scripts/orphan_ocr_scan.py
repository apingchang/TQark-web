"""Orphan OCR scan v4: 對 _未分類/ 內 orphan files, disk-based OCR + INSERT records + move.

Strategy v4 (William 13:23, 18:41 feedback):
- 對所有 orphan files (vector + image), 跑 OCR cover
- 嚴格 quality control: 只 INSERT records if OCR 抓到至少 county OR school_name (avoid garbled)
- OCR cover priority: pdftotext > fitz.get_text > tesseract rotation fallback
- 直書拆字 fix: pdftotext -layout 對直書 garbled → fitz.get_text 直書 reading order → text_compact
- INSERT 18 columns (county, level, school_name, version, ...)

SPEC 8/31:
- 直書 vector text file (e.g., 彰化縣_內安國小_四年級_112_下學期_第一次定期考_國語_翰林.pdf):
  - pdftotext fail (直書 layout garbled)
  - fitz.get_text() success (PyMuPDF 內建直書 reading order)
  - text 拆字 (vertical layout) → text_compact (去換行)
  - keyword match 抓對 county/school/year/term/exam/grade/subject
  - INSERT records + move to 彰化縣/國小/四年級/國語/

- Image-based files (純圖片,沒 vector text):
  - pdftotext + fitz.get_text() 都空
  - tesseract OCR (rotation 0, -90, 90, 180)
  - 慢 (30-60 sec/file), 對直書/黃紙可能 garbled

跳過:
- DriveFolder folder (source raw backup, OCR 會 fail 因為是亂排的)
- 109/ folder (orphan, 沒 drivefolder context)
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
from ocr_extract_metadata import (
    ocr_cover_robust, parse_ocr_text,
    detect_pdf_rotation, rotate_pdf_content,
)

ARCHIVE = ARCHIVE_ROOT
LOG_PATH = '/home/aping/.openclaw/workspace/.openclaw/tmp/orphan_ocr_scan.log'

_log_f = open(LOG_PATH, 'a', buffering=1)


def plog(*args):
    msg = ' '.join(str(a) for a in args)
    _log_f.write(msg + '\n')
    _log_f.flush()
    try:
        print(msg)
    except Exception:
        pass


def main():
    import fitz

    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    folder = Path('/mnt/my_book/考題收集/_未分類')
    pdf_files = []
    for entry in os.scandir(folder):
        if not entry.is_dir():
            continue
        if entry.name in ('DriveFolder', '109'):
            continue
        for root, dirs, files in os.walk(entry.path, followlinks=False):
            for f in files:
                if f.endswith('.pdf'):
                    pdf_files.append(Path(root) / f)

    cur.execute("SELECT rel_path FROM files WHERE rel_path LIKE '_未分類/%'")
    rel_paths_in_db = set(r['rel_path'] for r in cur.fetchall())
    orphan_files = []
    for pdf in pdf_files:
        rel = pdf.relative_to(ARCHIVE).as_posix()
        if rel not in rel_paths_in_db:
            orphan_files.append(pdf)
    plog(f'Total orphan files: {len(orphan_files)}')

    if not orphan_files:
        plog('No orphan files')
        return

    stats = {'rotated': 0, 'moved': 0, 'updated': 0, 'errors': 0, 'skip': 0, 'low_quality': 0}
    t0 = time.time()

    for i, abs_p in enumerate(orphan_files):
        rel_path = abs_p.relative_to(ARCHIVE).as_posix()
        filename = abs_p.name

        # OCR cover (with rotation detection)
        rotation = 0
        try:
            rotation = detect_pdf_rotation(abs_p)
        except Exception:
            rotation = 0

        if rotation != 0:
            try:
                rotate_pdf_content(abs_p, rotation=rotation)
                stats['rotated'] += 1
            except Exception:
                pass

        ocr_text = ''
        ocr_parsed = {}
        try:
            ocr_text, _ = ocr_cover_robust(abs_p)
            ocr_parsed = parse_ocr_text(ocr_text) if ocr_text else {}
        except Exception:
            ocr_parsed = {}

        # Extract OCR metadata
        ocr_county = normalize_county(ocr_parsed.get('county', ''))
        ocr_school_raw = ocr_parsed.get('school_name', '')

        # county 從 school_name 拆解 (例 「叩臺北市大同區太平國民」 → 臺北市)
        if not ocr_county and ocr_school_raw:
            try:
                from ocr_extract_metadata import COUNTIES as _COUNTIES
                for c in _COUNTIES:
                    if c in ocr_school_raw:
                        ocr_county = c
                        break
            except Exception:
                pass

        # Quality check: county 必須有 (從 OCR 或從 school_name 拆解)
        # 沒 county → LOW_QUALITY (records 不可信, 不 INSERT)
        if not ocr_county:
            stats['low_quality'] += 1
            plog(f'[{i+1}/{len(orphan_files)}] [LOW_QUALITY] {rel_path[:80]} (no county from OCR or school_name)')
            continue

        # Build metadata
        new_county = ocr_county or '_未分類'
        new_school = ocr_school_raw or '未標名'
        new_school = normalize_school_name(new_school, new_county)

        new_year = ocr_parsed.get('school_year', '') or ''
        new_year = normalize_year(new_year)

        new_term = ocr_parsed.get('school_term', '') or ''
        new_exam = ocr_parsed.get('exam_type', '') or '考試'
        new_grade = ocr_parsed.get('grade', '') or ''
        new_subject = ocr_parsed.get('subject', '').replace('科', '') or ''

        is_daan = '_daan' in filename or ocr_parsed.get('filetype') == 'daan'
        new_filetype = 'daan' if is_daan else 'paper'

        version_match = re.search(r'_(康軒|翰林|南一|何嘉仁|仁林|育橋)(?:_daan)?\.pdf$', filename)
        new_version = version_match.group(1) if version_match else (ocr_parsed.get('version') or '未註明')

        # Level
        if new_grade in ('一年級', '二年級', '三年級', '四年級', '五年級', '六年級'):
            lvl = '國小'
        elif new_grade in ('七年級', '八年級', '九年級'):
            lvl = '國中'
        elif new_grade in ('十年級', '十一年級', '十二年級'):
            lvl = '高中'
        else:
            lvl = ''

        final = {
            'county': new_county, 'school_name': new_school, 'school_year': new_year,
            'school_term': new_term, 'exam_type': new_exam, 'grade': new_grade,
            'subject': new_subject, 'level': '', 'filetype': new_filetype,
            'version': new_version,
        }
        new_rel = build_new_relpath(final, filetype=new_filetype)
        new_filename = build_new_filename(final, filetype=new_filetype)
        new_rel_full = f'{new_rel}/{new_filename}'
        new_abs = ARCHIVE / new_rel_full

        plog(f'[{i+1}/{len(orphan_files)}] {rel_path[:80]}')
        plog(f'  OCR: county={new_county} school={new_school} year={new_year} term={new_term} exam={new_exam} grade={new_grade} subject={new_subject}')
        plog(f'  → {new_rel_full}')

        if new_abs == abs_p:
            stats['skip'] += 1
        else:
            try:
                new_abs.parent.mkdir(parents=True, exist_ok=True)
                if new_abs.is_file():
                    plog(f'  target exists, skip')
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
                plog(f'  move error: {e}')
                stats['errors'] += 1
                continue

        # INSERT records (18 columns)
        try:
            with open(new_abs, 'rb') as f:
                content_hash = hashlib.sha256(f.read()).hexdigest()[:12]
            size_kb = new_abs.stat().st_size // 1024
            mtime = datetime.fromtimestamp(new_abs.stat().st_mtime).isoformat()
            has_school = 1 if new_school and new_school not in ('未標名', '') else 0
            cur.execute("""INSERT OR REPLACE INTO files
                (paper_id, rel_path, filename, county, level, school_year, school_term, exam_type, grade, subject, paper_or_daan, school_name, version, size_kb, mtime, is_paper, has_school, has_school_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (content_hash, new_rel_full, new_filename, new_county, lvl or '_未分類',
                 new_year or None, new_term or None, new_exam or None,
                 new_grade or None, new_subject or None, new_filetype, new_school or None,
                 new_version, size_kb, mtime,
                 1 if new_filetype == 'paper' else 0, has_school, 1 if new_school not in ('', '未標名') else 0))
            conn.commit()
            stats['updated'] += 1
        except Exception as e:
            plog(f'  records error: {e}')
            stats['errors'] += 1
            continue

        if (i+1) % 50 == 0:
            elapsed = time.time() - t0
            plog(f'\n[Progress {i+1}/{len(orphan_files)}] rotated={stats["rotated"]} moved={stats["moved"]} updated={stats["updated"]} errors={stats["errors"]} skip={stats["skip"]} low_quality={stats["low_quality"]} {elapsed:.0f}s')

    conn.close()
    plog(f'\n=== Final ===')
    plog(f'Rotated: {stats["rotated"]}')
    plog(f'Moved: {stats["moved"]}')
    plog(f'Updated (records INSERT): {stats["updated"]}')
    plog(f'Skip (same path or target exists): {stats["skip"]}')
    plog(f'Low quality (OCR failed, no insert): {stats["low_quality"]}')
    plog(f'Errors: {stats["errors"]}')
    plog(f'Total time: {time.time()-t0:.0f}s')


if __name__ == '__main__':
    main()