#!/usr/bin/env python3
"""
Stage 1 Migration: 從 _drivefolder filename 抽出 metadata 補進 DB 欄位

Filename format (apply v3 產生的):
  7 parts: <county>_<yr+term>_<exam>_<subject>_<grade>_<school>_<version>_drivefolder.pdf
  6 parts: <county>_<yr+term>_<subject>_<grade>_<school>_<version>_drivefolder.pdf (沒 exam)
  Ex: 新北市_101下學期_第一次段考_自然_七年級_新北市崇林國中_未註明_drivefolder.pdf
  Ex: 台南市_111上學期_公民_一年級_台南市復興國中_康軒_drivefolder.pdf

作者: 夥計 (William 指示) - 2026-08-26
"""
import argparse
import csv
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

DB_PATH = '/home/aping/MyProjects/TQark-web/backend/state/tqark-web.db'


def parse_filename(fname: str) -> dict:
    """從 _drivefolder filename 抽出欄位. Return dict 或 {}."""
    if not fname.endswith('_drivefolder.pdf'):
        return {}
    parts = fname[:-len('_drivefolder.pdf')].split('_')
    if len(parts) not in (6, 7):
        return {}

    result = {}
    if len(parts) == 7:
        # 7-part: [county, yr+term, exam, subject, grade, school, version]
        result['subject'] = parts[3]
        result['grade'] = parts[4]
        result['school'] = parts[5]
        yr = parts[1]
        if yr != '未註明':
            m = re.match(r'^(\d{3})(上學期|下學期)$', yr)
            if m:
                result['school_year'] = m.group(1)
                result['school_term'] = m.group(2)
            elif re.match(r'^\d{3}$', yr):
                result['school_year'] = yr
        if parts[2] != '未註明':
            result['exam_type'] = parts[2]
        if parts[6] != '未註明':
            result['version'] = parts[6]
    else:
        # 6-part: [county, yr+term, subject, grade, school, version] (沒 exam)
        result['subject'] = parts[2]
        result['grade'] = parts[3]
        result['school'] = parts[4]
        yr = parts[1]
        if yr != '未註明':
            m = re.match(r'^(\d{3})(上學期|下學期)$', yr)
            if m:
                result['school_year'] = m.group(1)
                result['school_term'] = m.group(2)
            elif re.match(r'^\d{3}$', yr):
                result['school_year'] = yr
        if parts[5] != '未註明':
            result['version'] = parts[5]

    return result


def fetch_drivefolder_records(conn):
    """抓所有 _drivefolder records"""
    cur = conn.cursor()
    cur.execute("""
        SELECT paper_id, rel_path, filename, school_year, school_term, exam_type, version
        FROM files
        WHERE rel_path LIKE '%_drivefolder.pdf'
    """)
    rows = cur.fetchall()
    return [dict(r) for r in rows]


def build_updates(records):
    """計算要 UPDATE 的 records. Return (updates, skipped_unparseable, skipped_no_new, stats)"""
    updates = []
    skipped_unparseable = []
    skipped_no_new = []
    stats = Counter()

    for r in records:
        fn = r['filename']
        parsed = parse_filename(fn)
        stats['total'] += 1
        if not parsed:
            skipped_unparseable.append((r['paper_id'], fn))
            stats['unparseable'] += 1
            continue

        year_new = parsed.get('school_year', '')
        term_new = parsed.get('school_term', '')
        exam_new = parsed.get('exam_type', '')
        ver_new = parsed.get('version', '')

        year_cur = r['school_year'] or ''
        term_cur = r['school_term'] or ''
        exam_cur = r['exam_type'] or ''
        ver_cur = r['version'] or ''

        year_to_set = year_new if (not year_cur and year_new) else None
        term_to_set = term_new if (not term_cur and term_new) else None
        exam_to_set = exam_new if (not exam_cur and exam_new) else None
        ver_to_set = ver_new if (not ver_cur and ver_new) else None

        if any([year_to_set, term_to_set, exam_to_set, ver_to_set]):
            updates.append({
                'paper_id': r['paper_id'],
                'filename': fn,
                'rel_path': r['rel_path'],
                'year_old': year_cur, 'year_new': year_to_set or year_cur,
                'term_old': term_cur, 'term_new': term_to_set or term_cur,
                'exam_old': exam_cur, 'exam_new': exam_to_set or exam_cur,
                'version_old': ver_cur, 'version_new': ver_to_set or ver_cur,
            })
            if year_to_set: stats['year_updated'] += 1
            if term_to_set: stats['term_updated'] += 1
            if exam_to_set: stats['exam_updated'] += 1
            if ver_to_set: stats['version_updated'] += 1
        else:
            skipped_no_new.append((r['paper_id'], fn))
            stats['no_new_info'] += 1

    return updates, skipped_unparseable, skipped_no_new, stats


def write_csv_preview(updates, csv_path):
    """寫 dry-run preview CSV"""
    with csv_path.open('w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['paper_id', 'filename', 'field', 'old', 'new'])
        for u in updates:
            for k in ['year', 'term', 'exam', 'version']:
                old = u[f'{k}_old']
                new = u[f'{k}_new']
                if old != new:
                    writer.writerow([u['paper_id'][:12], u['filename'][:60], k, old, new])


def apply_updates(conn, updates):
    """執行 UPDATE - 只在現有欄位空時填"""
    cur = conn.cursor()
    updated = 0
    for u in updates:
        cur.execute("""
            UPDATE files
            SET school_year = COALESCE(NULLIF(?, ''), school_year),
                school_term = COALESCE(NULLIF(?, ''), school_term),
                exam_type = COALESCE(NULLIF(?, ''), exam_type),
                version = COALESCE(NULLIF(?, ''), version)
            WHERE paper_id = ?
        """, (u['year_new'], u['term_new'], u['exam_new'], u['version_new'], u['paper_id']))
        updated += cur.rowcount
    conn.commit()
    return updated


def verify_metadata_density(conn):
    """驗證 _drivefolder metadata 填寫率"""
    cur = conn.cursor()
    cur.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN school_year != '' THEN 1 ELSE 0 END) as year,
            SUM(CASE WHEN school_term != '' THEN 1 ELSE 0 END) as term,
            SUM(CASE WHEN exam_type != '' THEN 1 ELSE 0 END) as exam,
            SUM(CASE WHEN version != '' THEN 1 ELSE 0 END) as ver
        FROM files
        WHERE rel_path LIKE '%_drivefolder.pdf'
    """)
    r = cur.fetchone()
    return dict(r)


def main():
    parser = argparse.ArgumentParser(description='Stage 1: 從 _drivefolder filename 補 metadata 進 DB')
    parser.add_argument('--apply', action='store_true', help='實際 UPDATE DB (default: dry-run)')
    parser.add_argument('--csv', type=Path, default=Path('/tmp/stage1_drivefolder_migration.csv'),
                        help='dry-run preview CSV 路徑')
    args = parser.parse_args()

    print(f'[Stage 1] {"APPLY" if args.apply else "DRY-RUN"} mode')
    print(f'[Stage 1] DB: {DB_PATH}')
    print()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    records = fetch_drivefolder_records(conn)
    print(f'[Stage 1] Fetched {len(records)} _drivefolder records')

    updates, skipped_unparseable, skipped_no_new, stats = build_updates(records)
    print(f'[Stage 1] Stats: {dict(stats)}')
    print(f'[Stage 1] Updates to apply: {len(updates)}')
    print(f'[Stage 1] Skipped unparseable: {len(skipped_unparseable)}')
    print(f'[Stage 1] Skipped no_new_info: {len(skipped_no_new)}')

    write_csv_preview(updates, args.csv)
    print(f'[Stage 1] CSV preview: {args.csv}')

    if updates:
        print('\n[Stage 1] First 5 updates preview:')
        for u in updates[:5]:
            print(f'  {u["filename"][:60]:<60}')
            for k in ['year', 'term', 'exam', 'version']:
                old = u[f'{k}_old'] or '(空)'
                new = u[f'{k}_new'] or '(空)'
                if old != new:
                    print(f'    {k}: {old} → {new}')

    if args.apply:
        updated = apply_updates(conn, updates)
        print(f'\n[Stage 1] ✅ Applied {updated} updates')
    else:
        print(f'\n[Stage 1] DRY-RUN: no changes. Re-run with --apply to UPDATE.')

    print('\n[Stage 1] === _drivefolder metadata 填寫率 ===')
    density = verify_metadata_density(conn)
    print(f'  total: {density["total"]}')
    for k in ['year', 'term', 'exam', 'ver']:
        filled = density[k]
        pct = filled / density['total'] * 100 if density['total'] else 0
        print(f'  {k}: {filled} / {density["total"]} ({pct:.1f}%)')

    conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
