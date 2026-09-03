"""Convert Word files (.doc/.docx) to PDF using LibreOffice headless.

Strategy:
- Read all .doc/.docx from _真word檔_待人工/ + 未分類/ (skip ~$ temp files)
- Parallel: spawn N LibreOffice workers (each with its own user profile)
- Output: keep same folder but with .pdf extension
- After conversion, OCR scan to extract metadata
"""
import os
import sys
import subprocess
import shutil
import hashlib
import sqlite3
import time
import json
import re
from pathlib import Path
from datetime import datetime
from concurrent.futures import ProcessPoolExecutor, as_completed
from multiprocessing import cpu_count

# Settings
INPUT_DIRS = [
    Path('/mnt/my_book/考題收集/_inbox/辨識不出/_真word檔_待人工/'),
    Path('/mnt/my_book/考題收集/_inbox/辨識不出/未分類/'),
]
LOG_PATH = '/home/aping/.openclaw/workspace/.openclaw/tmp/word_to_pdf.log'
DB_PATH = '/home/aping/MyProjects/TQark-web/backend/state/tqark-web.db'
MAX_PARALLEL = 4  # libreoffice can be heavy
SKIP_PATTERN = re.compile(r'^~\$')  # Office temp files

_log_f = open(LOG_PATH, 'a', buffering=1)


def plog(*args):
    msg = ' '.join(str(a) for a in args)
    _log_f.write(msg + '\n')
    _log_f.flush()
    try:
        print(msg, flush=True)
    except Exception:
        pass


def convert_one(args):
    """Convert one Word file to PDF. Returns (success, src_path, pdf_path, error)."""
    src_path, profile_dir = args
    src = Path(src_path)
    pdf_path = src.with_suffix('.pdf')

    if pdf_path.exists() and pdf_path.stat().st_size > 0:
        return ('skip', str(src), str(pdf_path), 'pdf exists')

    try:
        # Each libreoffice instance needs unique user profile
        result = subprocess.run(
            ['libreoffice', '--headless',
             f'-env:UserInstallation=file://{profile_dir}',
             '--convert-to', 'pdf',
             '--outdir', str(src.parent),
             str(src)],
            capture_output=True,
            timeout=60,
        )
        if pdf_path.exists() and pdf_path.stat().st_size > 0:
            return ('ok', str(src), str(pdf_path), '')
        else:
            return ('fail', str(src), '', result.stderr.decode('utf-8', errors='ignore')[:200])
    except subprocess.TimeoutExpired:
        return ('timeout', str(src), '', 'timeout 60s')
    except Exception as e:
        return ('error', str(src), '', str(e)[:200])


def get_unique_profile_dir(worker_id):
    """Create a unique libreoffice profile directory for each worker."""
    base = Path(f'/tmp/lo_profile_{worker_id}_{os.getpid()}')
    base.mkdir(parents=True, exist_ok=True)
    return base


def collect_files():
    """Collect all Word files from INPUT_DIRS."""
    files = []
    for input_dir in INPUT_DIRS:
        if not input_dir.exists():
            continue
        for root, dirs, filenames in os.walk(input_dir):
            for f in filenames:
                if SKIP_PATTERN.match(f):
                    continue
                if f.endswith(('.doc', '.docx')):
                    files.append(Path(root) / f)
    return files


def main():
    plog(f'=== word_to_pdf start: {datetime.now().isoformat()} ===')

    files = collect_files()
    plog(f'Total Word files to convert: {len(files)}')

    # Create profile dirs for each worker
    profile_dirs = [get_unique_profile_dir(i) for i in range(MAX_PARALLEL)]

    # Prepare args with rotating profile dirs
    args_list = [(str(f), profile_dirs[i % MAX_PARALLEL]) for i, f in enumerate(files)]

    stats = {'ok': 0, 'skip': 0, 'fail': 0, 'timeout': 0, 'error': 0}
    errors = []

    t0 = time.time()
    last_log = time.time()
    total = len(args_list)
    completed = 0

    with ProcessPoolExecutor(max_workers=MAX_PARALLEL) as executor:
        futures = {executor.submit(convert_one, args): args[0] for args in args_list}

        for future in as_completed(futures):
            status, src, pdf, err = future.result()
            stats[status] = stats.get(status, 0) + 1
            completed += 1

            if status in ('fail', 'timeout', 'error'):
                errors.append((src, err))

            now = time.time()
            if (completed % 50 == 0) or (now - last_log > 10):
                rate = completed / (now - t0 + 0.001)
                eta_min = (total - completed) / (rate + 0.001) / 60
                plog(f'[{completed}/{total}] {status}: rate={rate:.1f}/min eta={eta_min:.1f}min')
                last_log = now

    plog(f'\n=== Final ({(time.time()-t0)/60:.1f} min) ===')
    for k, v in sorted(stats.items()):
        plog(f'  {k}: {v}')

    if errors:
        plog(f'\nFirst 20 errors:')
        for src, err in errors[:20]:
            plog(f'  {src[:80]}: {err[:100]}')

    plog(f'=== Done: {datetime.now().isoformat()} ===')


if __name__ == '__main__':
    main()
