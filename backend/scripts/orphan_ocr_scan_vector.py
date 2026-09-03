"""Orphan OCR scan vector-only: 對 _未分類/ 內 vector text orphan files, OCR + INSERT records.

Strategy (William 8/31 9:58):
- 只OCR vector text files (fitz.get_text()有metadata or pdftotext有)
- image-based cover files SKIP (OCR garbled, records metadata不可信)
- 省時間,fast (0-2 sec/file)
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
)

ARCHIVE = ARCHIVE_ROOT
LOG_PATH = '/home/aping/.openclaw/workspace/.openclaw/tmp/orphan_ocr_scan_vector.log'

_log_f = open(LOG_PATH, 'a', buffering=1)


def plog(*args):
    msg = ' '.join(str(a) for a in args)
    _log_f.write(msg + '\n')
    _log_f.flush()
    try:
        print(msg)
    except Exception:
        pass


def classify_pdf_vector(abs_p):
    """Return True if vector text cover has metadata (學校/學年/考試/etc).
    Skip files > 30 MB (大 file = corrupted or high-res scanned).
    
    Strict check: cover (page 1) text 必須含 metadata keywords, 否則是 image-based cover → OCR會garbled.
    """
    try:
        if abs_p.stat().st_size > 30 * 1024 * 1024:
            return False
        import fitz
        with fitz.open(str(abs_p)) as doc:
            if len(doc) > 0:
                text = doc[0].get_text()
                if not text or len(text) < 50:
                    return False
                # 必須含至少 2 個 metadata keywords
                kws = ['學年度', '學年', '學期', '段考', '考試', '年級', '考', '學校', '國民', '縣立', '市立', '附設']
                kw_count = sum(1 for k in kws if k in text)
                return kw_count >= 2
    except Exception:
        pass
    return False


def main():
    import fitz

    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    folder = Path('/mnt/my_book/考題收集/_未分類')

    pdf_files = []
    for entry in os.scandir(folder):
        if not entry.is_dir() or entry.name in ('DriveFolder', '109'):
            continue
        for root, dirs, files in os.walk(entry.path, followlinks=False):
            for f in files:
                if f.endswith('.pdf'):
                    pdf_files.append(Path(root) / f)

    cur.execute("SELECT rel_path FROM files WHERE rel_path LIKE '_未分類/%'")
    rel_paths_in_db = set(r['rel_path'] for r in cur.fetchall())
    orphan_files = [p for p in pdf_files if p.relative_to(ARCHIVE).as_posix() not in rel_paths_in_db]

    # Filter to vector text only
    vector_files = []
    image_files = []
    for p in orphan_files:
        if classify_pdf_vector(p):
            vector_files.append(p)
        else:
            image_files.append(p)
    plog(f'Total orphan: {len(orphan_files)}, vector: {len(vector_files)}, image: {len(image_files)}')

    if not vector_files:
        plog('No vector orphan files')
        return

    stats = {'rotated': 0, 'moved': 0, 'updated': 0, 'errors': 0, 'skip': 0, 'low_quality': 0}
    t0 = time.time()

    for i, abs_p in enumerate(vector_files):
        rel_path = abs_p.relative_to(ARCHIVE).as_posix()
        filename = abs_p.name

        # OCR cover (vector text, fast) - skip if file too large
        if abs_p.stat().st_size > 30 * 1024 * 1024:
            plog(f'[{i+1}/{len(vector_files)}] [SKIP_LARGE] {rel_path[:80]} (size: {abs_p.stat().st_size//1024//1024}MB)')
            stats['skip'] += 1
            continue

        # OCR cover (vector text, fast)
        ocr_text = ''
        ocr_parsed = {}
        try:
            ocr_text, _ = ocr_cover_robust(abs_p)
            ocr_parsed = parse_ocr_text(ocr_text) if ocr_text else {}
        except Exception:
            ocr_parsed = {}

        # Extract county (from OCR or school_name)
        ocr_county = normalize_county(ocr_parsed.get('county', ''))
        if not ocr_county and ocr_parsed.get('school_name'):
            try:
                from ocr_extract_metadata import COUNTIES as _COUNTIES
                for c in _COUNTIES:
                    if c in ocr_parsed['school_name']:
                        ocr_county = c
                        break
            except Exception:
                pass

        if not ocr_county:
            stats['low_quality'] += 1
            if i < 20:
                plog(f'[{i+1}/{len(vector_files)}] [LOW_QUALITY] {rel_path[:80]} (no county)')
            continue

        new_county = ocr_county
        new_school = ocr_parsed.get('school_name', '') or '未標名'
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
        new_version = version_match.group(1) if version_match else '未註明'

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

        plog(f'[{i+1}/{len(vector_files)}] {rel_path[:80]}')
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
                shutil.move(str(abs_p), new_abs)
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

        try:
            with open(new_abs, 'rb') as f:
                content_hash = hashlib.sha256(f.read()).hexdigest()[:12]
            size_kb = new_abs.stat().st_size // 1024
            mtime = datetime.fromtimestamp(new_abs.stat().st_mtime).isoformat()
            cur.execute("""INSERT OR REPLACE INTO files
                (paper_id, rel_path, filename, county, level, school_year, school_term, exam_type, grade, subject, paper_or_daan, school_name, version, size_kb, mtime, is_paper, has_school, has_school_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (content_hash, new_rel_full, new_filename, new_county, lvl or '_未分類',
                 new_year or None, new_term or None, new_exam or None,
                 new_grade or None, new_subject or None, new_filetype, new_school or None,
                 new_version, size_kb, mtime,
                 1 if new_filetype == 'paper' else 0, 1 if new_school not in ('', '未標名') else 0, 1 if new_school not in ('', '未標名') else 0))
            conn.commit()
            stats['updated'] += 1
        except Exception as e:
            plog(f'  records error: {e}')
            stats['errors'] += 1
            continue

        if (i+1) % 20 == 0:
            elapsed = time.time() - t0
            plog(f'\n[Progress {i+1}/{len(vector_files)}] rotated={stats["rotated"]} moved={stats["moved"]} updated={stats["updated"]} errors={stats["errors"]} skip={stats["skip"]} low_quality={stats["low_quality"]} {elapsed:.0f}s')

    conn.close()
    plog(f'\n=== Final ===')
    plog(f'Vector files: {len(vector_files)}')
    plog(f'Rotated: {stats["rotated"]}')
    plog(f'Moved: {stats["moved"]}')
    plog(f'Updated: {stats["updated"]}')
    plog(f'Skip: {stats["skip"]}')
    plog(f'Low quality (no county): {stats["low_quality"]}')
    plog(f'Errors: {stats["errors"]}')
    plog(f'Total time: {time.time()-t0:.0f}s')


if __name__ == '__main__':
    main()
