#!/usr/bin/env python3
"""DriveFolder inbox OCR scan (2026-09-05)

處理 _inbox/_未分類/DriveFolder/ 裡剩下的 PDF
Path pattern: _inbox/_未分類/DriveFolder/<county>/<school>/<year>/<exam_folder>/<subject>/<file>
"""

import re
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, '/home/aping/MyProjects/TQark-web/backend/scripts')
from inbox_ocr_scan_v2 import (
    parse_filename_advanced,
    is_daan_filename,
    normalize_subject,
    normalize_school_name,
    normalize_year,
    build_new_filename,
    build_new_relpath,
    get_level,
)

ARCHIVE = Path('/mnt/my_book/考題收集')
DB_PATH = Path('/home/aping/MyProjects/TQark-web/backend/state/tqark-web.db')
DRIVE_ROOT = ARCHIVE / '_inbox' / '_未分類' / 'DriveFolder'


def parse_exam_folder(folder_name):
    out = {}
    digit_map = {'一': '一', '二': '二', '三': '三', '1': '一', '2': '二', '3': '三'}
    m = re.match(r'(\d{2,3})(上|下)學期第?(一|二|三|1|2|3)次(段考)?', folder_name)
    if m:
        out['school_year'] = normalize_year(m.group(1))
        out['school_term'] = f'{m.group(2)}學期'
        out['exam_type'] = f'第{digit_map.get(m.group(3), m.group(3))}次段考'
        return out
    m = re.match(r'(\d{2,3})(上|下)學期', folder_name)
    if m:
        out['school_year'] = normalize_year(m.group(1))
        out['school_term'] = f'{m.group(2)}學期'
        return out
    m = re.match(r'(\d{2,3})(下|上)?(一|二|三|1|2|3)段', folder_name)
    if m:
        out['school_year'] = normalize_year(m.group(1))
        out['school_term'] = f'{m.group(2) or "下"}學期'
        out['exam_type'] = f'第{digit_map.get(m.group(3), m.group(3))}次段考'
        return out
    return out


def parse_drivefolder_path(rel_path):
    parts = rel_path.split('/')
    if len(parts) < 9 or parts[:3] != ['_inbox', '_未分類', 'DriveFolder']:
        return None

    county = parts[3]
    school = parts[4]
    year_folder = parts[5]
    exam_folder = parts[6]
    subject_folder = parts[7] if len(parts) > 8 else None
    filename = parts[-1]

    if '_converted' in parts or filename.startswith('.~lock') or filename.endswith('.tmp'):
        return None
    if not filename.lower().endswith('.pdf'):
        return None

    out = {
        'county': county,
        'school_name': school,
        'subject': normalize_subject(subject_folder) if subject_folder else None,
    }

    m = re.match(r'(\d{2,3})學年度', year_folder)
    if m:
        out['school_year'] = normalize_year(m.group(1))

    ef_parsed = parse_exam_folder(exam_folder)
    out.update({k: v for k, v in ef_parsed.items() if v})

    return out


def process_pdf(abs_p, rel_path):
    filename = abs_p.name
    parsed = {}

    fn_parsed = parse_filename_advanced(filename)
    parsed.update({k: v for k, v in fn_parsed.items() if v})

    path_parsed = parse_drivefolder_path(rel_path)
    if not path_parsed:
        return ('skip_unsupported', rel_path, 'path not in DriveFolder pattern')

    parsed['county'] = path_parsed['county']
    if path_parsed.get('school_name'):
        parsed['school_name'] = path_parsed['school_name']
    for k in ('school_year', 'school_term', 'exam_type'):
        if path_parsed.get(k) and not parsed.get(k):
            parsed[k] = path_parsed[k]
    if path_parsed.get('subject') and not parsed.get('subject'):
        parsed['subject'] = path_parsed['subject']

    if parsed.get('school_name') and parsed.get('county') and parsed['county'] != '_未分類':
        parsed['school_name'] = normalize_school_name(parsed['school_name'], parsed['county'])

    is_daan = is_daan_filename(filename)
    filetype = 'daan' if is_daan else 'paper'
    parsed['paper_or_daan'] = filetype

    if parsed.get('subject') == '社會':
        for sub in ['公民', '地理', '歷史']:
            if sub in filename:
                parsed['subject'] = sub
                break

    required = ['county', 'grade', 'subject', 'school_year']
    missing = [r for r in required if not parsed.get(r)]
    if missing:
        return ('skip_low_quality', rel_path,
                f'missing: {missing}, parsed: {parsed}')

    level = get_level(parsed['grade'])

    final = {
        'county': parsed['county'],
        'school_name': parsed.get('school_name', ''),
        'school_year': parsed.get('school_year', ''),
        'school_term': parsed.get('school_term', ''),
        'exam_type': parsed.get('exam_type', ''),
        'grade': parsed['grade'],
        'subject': parsed['subject'],
        'level': level,
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
        return ('skip_io_error', rel_path, f'move failed: {e}')

    conn = sqlite3.connect(str(DB_PATH))
    try:
        cur = conn.cursor()
        size_kb = new_abs.stat().st_size // 1024
        cur.execute("SELECT paper_id FROM files WHERE rel_path = ?", (rel_path,))
        row = cur.fetchone()
        paper_id = row[0] if row else None

        mtime = datetime.fromtimestamp(new_abs.stat().st_mtime).isoformat()

        cur.execute("""
            INSERT OR REPLACE INTO files (
                paper_id, rel_path, filename, county, level, school_year, school_term,
                exam_type, grade, subject, paper_or_daan, school_name, version,
                size_kb, mtime, is_paper, has_school, has_school_name
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            paper_id, new_rel_full, new_filename,
            final['county'], final['level'], final['school_year'], final['school_term'],
            final['exam_type'], final['grade'], final['subject'],
            final['filetype'], final['school_name'], final['version'],
            size_kb, mtime,
            1 if final['filetype'] == 'paper' else 0,
            1 if final['school_name'] else 0,
            1 if final['school_name'] else 0,
        ))
        conn.commit()
    except Exception as e:
        conn.rollback()
        return ('skip_db_error', rel_path, f'db insert failed: {e}')
    finally:
        conn.close()

    return ('moved_inserted', new_rel_full,
            f'county={final["county"]} school={final["school_name"]} grade={final["grade"]} year={final["school_year"]} term={final["school_term"]} exam={final["exam_type"]} subject={final["subject"]} filetype={final["filetype"]}')


def main():
    pdfs = []
    for p in DRIVE_ROOT.rglob('*.pdf'):
        if '._' in p.name or '.~lock' in p.name or p.name.endswith('.tmp'):
            continue
        rel = str(p.relative_to(ARCHIVE))
        pdfs.append((p, rel))

    print(f'Found {len(pdfs)} PDFs in DriveFolder')

    counts = {}
    for i, (abs_p, rel) in enumerate(pdfs, 1):
        action, new_rel, msg = process_pdf(abs_p, rel)
        counts[action] = counts.get(action, 0) + 1
        print(f'[{i}/{len(pdfs)}] {action}: {msg}')

    print('\n=== Summary ===')
    for action, count in sorted(counts.items()):
        print(f'  {action}: {count}')


if __name__ == '__main__':
    main()
