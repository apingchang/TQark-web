"""
Migrate 現有 PDFs from 舊結構 (level/grade/subject/paper) 到 county 結構。

1. 掃描所有 PDF
2. 從 filename + PDF title 拿 county + 完整學校名
3. 移動到 county/level/grade/subject/paper/ 結構
4. 重新命名: <county>_<year>_<exam>_<fileid>_<school>_<version>.pdf
5. 其他X_ prefix 給 county unknown

⚠️ 這個 script 會真的 move 檔案!
"""
import sys
import shutil
import re
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.scraper.archive_path import (
    build_archive_filename,
    normalize_county,
    UNKNOWN_COUNTY,
    SEGMENT_MAP,
    _safe_dirname,
)
from app.scraper.pdf_title import extract_school_from_pdf

ARCHIVE_DIR = Path("/mnt/my_book/考題收集")


def main(dry_run: bool = False):
    print(f"{'[DRY RUN] ' if dry_run else ''}Migrating PDFs in {ARCHIVE_DIR}...")
    
    moved = 0
    skipped = 0
    errors = 0
    
    # 先掃描所有 PDF (只保留還在 國小/國中/高中 底下的 = 未遷移)
    pdf_files = []
    for pdf_path in ARCHIVE_DIR.rglob("*.pdf"):
        rel = pdf_path.relative_to(ARCHIVE_DIR)
        first_part = rel.parts[0]
        # 只處理還沒遷移的 (在 國小/國中/高中 底下)
        if first_part not in SEGMENT_MAP.values():
            continue
        pdf_files.append(pdf_path)
    
    print(f"Found {len(pdf_files)} PDFs to migrate")
    
    for pdf_path in pdf_files:
        # 從 path 拆出 grade + subject + filetype
        parts = pdf_path.parts
        # 國小/一年級/生活/paper/<file>.pdf
        if "paper" in parts:
            filetype = "paper"
        elif "daan" in parts:
            filetype = "daan"
        else:
            skipped += 1
            continue
        
        archive_root_parts = len(ARCHIVE_DIR.parts)
        grade = parts[archive_root_parts + 1]  # 國小/grade/...
        subject = parts[archive_root_parts + 2]  # 國小/grade/subject/...
        
        # 從 filename parse fileid
        filename_stem = pdf_path.stem
        fname_parts = filename_stem.split("_")
        try:
            fileid = fname_parts[2]
            year_term = fname_parts[0]
            exam_type = fname_parts[1]
            version = fname_parts[-1] if len(fname_parts) >= 5 else "未註明"
            school_short = "_".join(fname_parts[3:-1]) if len(fname_parts) >= 5 else "未註明"
        except IndexError:
            errors += 1
            continue
        
        # 從 PDF title 抓 county + 完整學校名 (OCR fallback)
        try:
            pdf_info = extract_school_from_pdf(pdf_path)
            effective_county = pdf_info["county"] if pdf_info["county"] not in ("未註明", "其他縣市") else None
            effective_school = pdf_info["school_name"] if pdf_info["school_name"] != "未註明" else school_short
        except Exception:
            effective_county = None
            effective_school = school_short
        
        # 組正式檔名 + path
        new_filename = build_archive_filename(
            county=effective_county,
            year_term=year_term,
            exam_type=exam_type,
            fileid=fileid,
            school_name=effective_school,
            version=version,
        )
        
        county_norm = normalize_county(effective_county)
        safe_subject = _safe_dirname(subject)
        new_path = (
            ARCHIVE_DIR / county_norm
            / SEGMENT_MAP[grade] / grade / safe_subject / filetype
            / f"{new_filename}.pdf"
        )
        
        if new_path == pdf_path:
            skipped += 1
            continue
        
        if not dry_run:
            try:
                new_path.parent.mkdir(parents=True, exist_ok=True)
                if new_path.exists():
                    # 已經存在 (e.g. daan + paper 同 fileid), skip
                    print(f"  ! skip (target exists): {new_path.relative_to(ARCHIVE_DIR)}")
                    skipped += 1
                    # 但把原檔刪掉 (因為被取代)
                    pdf_path.unlink()
                    continue
                shutil.move(str(pdf_path), str(new_path))
                moved += 1
            except OSError as e:
                print(f"  ✗ error: {pdf_path.relative_to(ARCHIVE_DIR)} → {e}")
                errors += 1
        else:
            print(f"  → {pdf_path.relative_to(ARCHIVE_DIR)} → {new_path.relative_to(ARCHIVE_DIR)}")
            moved += 1
    
    print(f"\n{'[DRY RUN] ' if dry_run else ''}Done:")
    print(f"  Moved:   {moved}")
    print(f"  Skipped: {skipped}")
    print(f"  Errors:  {errors}")


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    main(dry_run=dry_run)