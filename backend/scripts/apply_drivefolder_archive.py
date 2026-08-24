#!/usr/bin/env python3
"""
Drive folder archive apply (2026-08-24)

任務: 把 _未分類/DriveFolder/ 內 HIGH confidence 的 PDF
      歸檔到 <county>/<level>/<grade>/<subject>/<paper|daan>/ 結構
      從 dryrun 分析 (drivefolder_dryrun.py) 拿 target_rel/path

⚠️ Phase 1 only — HIGH confidence only
🚫 MEDIUM/LOW 不搬 (會被 derive_confidence() skip)
✅ target exists + size 一樣 → 刪 source (dedup)
🚫 macOS metadata (._*) 自動過濾
✅ shutil.copy2 保留 mtime + os.remove() 刪 source

Usage:
  source .venv/bin/activate
  uv run python scripts/apply_drivefolder_archive.py --date 20260824
  uv run python scripts/apply_drivefolder_archive.py --date 20260824 --apply
  uv run python scripts/apply_drivefolder_archive.py --date 20260824 --apply --only-school 新北市崇林國中
"""

import argparse
import csv
import json
import os
import shutil
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path("/home/aping/MyProjects/TQark-web")
ARCHIVE_ROOT = Path("/mnt/my_book/考題收集")
ANALYSIS_DIR = ROOT / "backend" / "scripts" / "analysis"


def load_dryrun(date_str):
    """Load pre-computed dry-run analysis CSV."""
    csv_path = ANALYSIS_DIR / f"drivefolder_dryrun_{date_str}.csv"
    if not csv_path.exists():
        raise FileNotFoundError(
            f"Dry-run analysis not found: {csv_path}\n"
            f"Run first: uv run python scripts/analysis/drivefolder_dryrun.py --date {date_str}"
        )
    with csv_path.open() as f:
        return list(csv.DictReader(f))


def is_macos_metadata(filename):
    """macOS ._* metadata files (AppleDouble) — should be filtered."""
    return filename.startswith("._")


def filter_targets(items, only_school=None, include_medium=False):
    """Filter dry-run items to those we'll actually move.
    Returns (targets, skip_reasons, skipped_macos).
    """
    skip_reasons = Counter()
    skipped_macos = 0
    targets = []

    for item in items:
        filename = item["filename"]
        if is_macos_metadata(filename):
            skipped_macos += 1
            continue

        confidence = item["confidence"]
        if confidence == "HIGH":
            pass  # Phase 1: always include
        elif confidence == "MEDIUM" and include_medium:
            pass  # Phase 2: only with --include-medium flag
        elif confidence == "LOW":
            skip_reasons["low_confidence"] += 1
            continue
        else:
            skip_reasons[f"skip_{confidence.lower()}"] += 1
            continue

        if only_school and item["school_name"] != only_school:
            skip_reasons["only_school_filter"] += 1
            continue

        targets.append(item)

    return targets, skip_reasons, skipped_macos


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date",
        required=True,
        help="Date string of pre-computed dry-run (e.g., 20260824)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually move files (default is dry-run)",
    )
    parser.add_argument(
        "--only-school",
        default=None,
        help="Apply only this school (format: 縣市校名, e.g., '新北市崇林國中')",
    )
    parser.add_argument(
        "--include-medium",
        action="store_true",
        help="Phase 2: also include MEDIUM confidence items",
    )
    args = parser.parse_args()

    dry_run = not args.apply
    items = load_dryrun(args.date)
    print(f"[apply] Loaded {len(items):,} dry-run items from {args.date}", flush=True)
    print(f"[apply] Mode: {'DRY-RUN (no changes)' if dry_run else 'APPLY (moving files)'}", flush=True)

    targets, skip_reasons, skipped_macos = filter_targets(
        items,
        only_school=args.only_school,
        include_medium=args.include_medium,
    )

    print(f"[apply] Targets after filter: {len(targets):,}")
    print(f"[apply] Skipped: {dict(skip_reasons)}")
    if skipped_macos:
        print(f"[apply] Skipped {skipped_macos} macOS metadata files (._*)")

    if not targets:
        print("[apply] No targets to move. Exiting.")
        return 0

    if dry_run:
        # Dry-run: just print first 5 + counts, no I/O
        print(f"\n[apply] DRY-RUN preview (first 5 of {len(targets):,}):")
        for t in targets[:5]:
            print(f"  {t['abs_path']}")
            print(f"    -> {t['target_rel']}")
        print()
        print(f"[apply] Would move: {len(targets):,}")
        print(f"⚠️  DRY-RUN: nothing was changed. Re-run with --apply to actually move files.")
        return 0

    # Real apply mode
    moved = 0
    dedup_deleted = 0  # target 已存在且 size 一樣 → 刪 source (重複清理)
    failed = 0
    fail_messages = []
    for item in targets:
        abs_path = Path(item["abs_path"])
        target_abs = ARCHIVE_ROOT / item["target_rel"]

        if not abs_path.exists():
            failed += 1
            if len(fail_messages) < 5:
                fail_messages.append(f"source missing: {abs_path}")
            continue

        if target_abs.exists():
            # 8/25 fix: size 比對 dedup
            try:
                src_size = abs_path.stat().st_size
                tgt_size = target_abs.stat().st_size
                if src_size == tgt_size:
                    # 重複內容, 刪 source
                    os.remove(abs_path)
                    dedup_deleted += 1
                    continue
                else:
                    # 同 target_rel 不同 size = 不同 PDF, 不能 overwrite
                    failed += 1
                    if len(fail_messages) < 5:
                        fail_messages.append(
                            f"target exists, size mismatch (src={src_size}, tgt={tgt_size}): {target_abs}"
                        )
                    continue
            except OSError as e:
                failed += 1
                if len(fail_messages) < 5:
                    fail_messages.append(f"stat error: {e} ({target_abs})")
                continue

        target_abs.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(abs_path, target_abs)
            os.remove(abs_path)
        except OSError as e:
            failed += 1
            if len(fail_messages) < 5:
                fail_messages.append(f"I/O error: {e} ({abs_path})")
            continue

        moved += 1
        if moved % 50 == 0:
            print(f"  [progress] {moved}/{len(targets)}", flush=True)

    print()
    print(f"[apply] Moved: {moved:,}")
    print(f"[apply] Deduped (target exists, size match, source deleted): {dedup_deleted:,}")
    if failed:
        print(f"[apply] Failed: {failed:,}")
        for msg in fail_messages:
            print(f"  {msg}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
