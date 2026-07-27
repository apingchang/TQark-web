#!/usr/bin/env python3
"""
Migrate tcool schools archive to StudyArk-like structure
2026-07-27 William: 把 /mnt/my_book/考題收集/<county>/<school>/<學期>/file.pdf
              改成 /mnt/my_book/考題收集/<county>/<level>/<grade>/<subject>/<filetype>/<file.pdf>

跟 StudyArk 一致,讓 TQark-web search UI / 平台資訊 找得到。

Source structure:
  /mnt/my_book/考題收集/<county>/<school>/<學期>/*.pdf

Target structure:
  /mnt/my_book/考題收集/<county>/<level>/<grade>/<subject>/<filetype>/<file>.pdf

Filename parsing (e.g., "111學年度第1學期一年級英語補考題庫.docx"):
  year=111, term=1, grade=一年級, subject=英語, exam_type=補考題庫
  + has_answer (with 答案/解答/解析) → filetype=daan else paper

Usage:
  uv run python scripts/migrate_tcool_to_studyark_structure.py [--dry-run]
"""

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).parent.parent

ARCHIVE_ROOT = Path(os.environ.get("TQARK_ARCHIVE_DIR", "/mnt/my_book/考題收集"))


# === School name → level ===
# 從外部 sources 拿 segment hint (高中/國中/國小)
def load_school_levels():
    """Read external_sources.json, build {school_name: level} map"""
    sources_file = BACKEND_DIR / "data" / "external_sources.json"
    if not sources_file.exists():
        return {}
    data = json.loads(sources_file.read_text(encoding="utf-8"))
    levels = {}
    for s in data.get("schools", []):
        name = s.get("name", "")
        url = s.get("url", "")
        # Determine level from school name
        if "高中" in name or "高級中學" in name or "高商" in name or "高工" in name or "高醫" in name:
            level = "高中"
        elif "國中" in name or "國民中學" in name or "完全中學" in name:
            level = "國中"
        elif "國小" in name or "國民小學" in name:
            level = "國小"
        else:
            level = "其他"
        if name:
            levels[name] = level
    return levels


# === Parse filename ===
def parse_filename(fname):
    """
    Parse 檔名提取 year, term, grade, subject, exam_type, has_answer
    Example: "111學年度第1學期一年級英語補考題庫.docx"
    Returns dict or None if can't parse
    """
    base = Path(fname).stem

    # Year + term — 多種格式:
    #   111學年度第1學期
    #   111學年度第一學期
    #   111學年第1學期 (沒有度)
    #   111學年度_第2學期 (有底線)
    #   111學年度第1_學期 (中間底線)
    yterm_match = re.search(
        r'(\d{3})學年(?:度)?(?:[_、\s])?[第]?(\d|[一二三四])(?:[_、\s])?學期',
        base
    )
    if not yterm_match:
        return None
    year = int(yterm_match.group(1))
    term_raw = yterm_match.group(2)
    if term_raw.isdigit():
        term = int(term_raw)
    else:
        # 第一/第二/第三/第四學期 → 1/2/3/4
        term_map = {"一": 1, "二": 2, "三": 3, "四": 4}
        term = term_map.get(term_raw, 1)

    # Grade: 一年級, 二年級, ..., 九年級 OR 國一/國二/國三/國中三年級 etc
    grade = None
    grade_match = re.search(r'([一二三四五六七八九](?:[_、\s])?年級)', base)
    if grade_match:
        grade = grade_match.group(1).replace("_", "").replace("、", "").replace(" ", "")
    else:
        # 國N 形式
        guo_match = re.search(r'國([一二三])', base)
        if guo_match:
            grade_map = {"一": "一年級", "二": "二年級", "三": "三年級"}
            grade = grade_map.get(guo_match.group(1))
        else:
            # 國N
            san_match = re.search(r'國三', base)
            if san_match:
                grade = "三年級"

    if not grade:
        return None

    # Subject: 年級後到「補考題庫」「段考」前的部分
    # Example: "111學年度第1學期一年級英語補考題庫" → subject = "英語"
    after_grade = base[grade_match.end():] if grade_match else ""
    # Remove 常見 suffix: 補考, 段考, 範圍, 題庫, 考卷, 試題
    # 以及底線、空格 (e.g., "國語文_" → "國語文")
    subj_match = re.match(r'([^\s補段範題考_]+?)(?:[_]?(?:補考|段考|範圍|題庫|考卷|試題)|$)', after_grade)
    if subj_match:
        subject = subj_match.group(1).strip().rstrip("_")
    else:
        subject = "其他"

    # Exam type: 補考題庫 / 段考 / etc
    if "補考題庫" in base or "補考範圍" in base or "補考題" in base:
        exam_type = "補考"
    elif "段考" in base:
        exam_type = "段考"
    elif "期末考" in base:
        exam_type = "期末考"
    elif "期中考" in base:
        exam_type = "期中考"
    else:
        exam_type = "其他"

    # Has answer (daan)?
    has_answer = bool(re.search(r'(答案|解答|解析|詳解)', base))

    return {
        "year": year,
        "term": term,
        "grade": grade,
        "subject": subject,
        "exam_type": exam_type,
        "has_answer": has_answer,
    }


def migrate_file(src_path: Path, school_levels: dict, dry_run=False, log=None):
    """
    Migrate single file to StudyArk-like structure
    Returns (target_path, status) tuple
    """
    if log is None:
        log = print

    if not src_path.is_file():
        return None, "not_found"

    # Walk up to find school dir: <archive>/<county>/<school>/<學期>/file
    rel = src_path.relative_to(ARCHIVE_ROOT)
    parts = rel.parts
    if len(parts) < 3:
        return None, "depth_too_shallow"

    county = parts[0]
    school = parts[1]

    # Skip non-tcool folders (e.g., already-studyark-structure)
    if school in ("國中", "高中", "國小"):
        return None, "already_migrated"
    if "其他" in school or "考題" in school:
        return None, "skip_meta"

    # School level from name
    level = school_levels.get(school, "其他")
    if level == "其他":
        # Try to infer from filename grade
        info = parse_filename(src_path.name)
        if info:
            grade = info["grade"]
            if grade in ("四年級", "五年級", "六年級"):
                level = "國小"
            elif grade in ("七年級", "八年級", "九年級"):
                level = "國中"
            elif grade in ("一年級", "二年級", "三年級"):
                # Year 1-3 is ambiguous: 高中 or 國中 (both use these)
                # Default to 高中 since high school uses these primarily
                level = "高中"
            else:
                level = "其他"

    if level == "其他":
        return None, "unknown_level"

    # Parse filename
    info = parse_filename(src_path.name)
    if not info:
        return None, "parse_failed"

    grade = info["grade"]
    subject = info["subject"]
    filetype = "daan" if info["has_answer"] else "paper"

    # Build new filename
    ext = src_path.suffix  # .pdf, .docx, .doc
    # Format: <county>_<3-digit-year>_<term>_<exam-type>_<school>_<grade>_<subject>[_答案].pdf
    new_name_parts = [
        county,
        f"{info['year']:03d}",
        f"第{info['term']}學期",
        info['exam_type'],
        school,
        grade,
        subject,
    ]
    if info['has_answer']:
        new_name_parts.append("解答")
    new_name = "_".join(new_name_parts) + ext

    # Build target path
    target_dir = ARCHIVE_ROOT / county / level / grade / subject / filetype
    target_path = target_dir / new_name

    if dry_run:
        log(f"  [dry-run] {src_path.relative_to(ARCHIVE_ROOT)} → {target_path.relative_to(ARCHIVE_ROOT)}")
        return target_path, "dry_run"

    # Move
    target_dir.mkdir(parents=True, exist_ok=True)
    if target_path.exists():
        # Same target — likely already migrated or duplicate
        log(f"  [skip] {new_name} already exists at {target_path.relative_to(ARCHIVE_ROOT)}")
        return target_path, "exists"

    shutil.move(str(src_path), str(target_path))
    log(f"  [✓] {src_path.name} → {target_path.relative_to(ARCHIVE_ROOT)}")
    return target_path, "moved"


def main():
    parser = argparse.ArgumentParser(description="Migrate tcool archive to StudyArk structure")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without moving")
    args = parser.parse_args()

    log = print  # simple logger

    school_levels = load_school_levels()
    log(f"Loaded {len(school_levels)} school levels")

    # Walk archive, find tcool-like structure
    stats = {"moved": 0, "dry_run": 0, "exists": 0, "skip": 0, "errors": []}
    moved_paths = []

    for county_dir in ARCHIVE_ROOT.iterdir():
        if not county_dir.is_dir():
            continue
        if county_dir.name in ("cap_exam", "ceec", "logs", "state", "其他X"):
            continue
        county = county_dir.name

        for school_dir in county_dir.iterdir():
            if not school_dir.is_dir():
                continue
            # Skip StudyArk-structure folders
            if school_dir.name in ("國中", "高中", "國小"):
                continue

            school = school_dir.name

            # Walk inside school
            for f in school_dir.rglob("*"):
                if not f.is_file():
                    continue
                if f.suffix.lower() not in (".pdf", ".docx", ".doc"):
                    continue

                target, status = migrate_file(f, school_levels, dry_run=args.dry_run, log=log)
                if status in ("moved", "dry_run", "exists"):
                    stats[status] = stats.get(status, 0) + 1
                else:
                    stats["skip"] += 1
                    if status not in ("unknown_level", "parse_failed", "already_migrated", "not_found", "depth_too_shallow"):
                        stats["errors"].append(f"{f}: {status}")

    log("")
    log("=== Summary ===")
    log(f"  Moved:       {stats.get('moved', 0)}")
    log(f"  Dry-run:      {stats.get('dry_run', 0)}")
    log(f"  Already exist: {stats.get('exists', 0)}")
    log(f"  Skipped:      {stats.get('skip', 0)}")
    if stats["errors"]:
        log(f"  Errors: {len(stats['errors'])}")
        for e in stats["errors"][:10]:
            log(f"    {e}")


if __name__ == "__main__":
    main()