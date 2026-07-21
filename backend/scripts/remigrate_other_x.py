"""
Re-migrate 其他X/ 內的 PDF — 對每個 PDF 用 OCR 補抓 county, 然後搬到正確 folder。

只處理 其他X/ 內的 PDF (其他不動)。
"""
import sys
import shutil
from pathlib import Path

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


def main():
    print(f"Re-migrating PDFs in {ARCHIVE_DIR / UNKNOWN_COUNTY}/")
    
    other_x_dir = ARCHIVE_DIR / UNKNOWN_COUNTY
    pdf_files = list(other_x_dir.rglob("*.pdf"))
    print(f"Found {len(pdf_files)} PDFs in 其他X/")
    
    moved = 0
    stayed = 0
    errors = 0
    
    for pdf_path in pdf_files:
        parts = pdf_path.relative_to(other_x_dir).parts
        if "paper" in parts:
            filetype = "paper"
        elif "daan" in parts:
            filetype = "daan"
        else:
            errors += 1
            continue
        
        # parts: 國中/七年級/數學/paper/<file>.pdf
        if len(parts) < 5:
            errors += 1
            continue
        
        # UNKNOWN_COUNTY/segment/grade/subject/filetype/<filename>
        # 但 parts 是 relative to other_x_dir, 所以 parts[0]=segment, parts[1]=grade, parts[2]=subject
        segment = parts[0]
        grade = parts[1]
        subject = parts[2]
        
        # 從 filename parse fileid + year_term + exam_type + school_short + version
        filename_stem = pdf_path.stem
        # 其他X_111_期中考_36273_縣立福興國中_翰林
        # 但這個 filename 已經有 其他X_ prefix (因為是其他X/ 內的)
        # 我們重新 parse: 去掉 其他X_ prefix
        if filename_stem.startswith(UNKNOWN_COUNTY + "_"):
            stripped = filename_stem[len(UNKNOWN_COUNTY) + 1:]
        else:
            stripped = filename_stem
        
        fname_parts = stripped.split("_")
        if len(fname_parts) < 5:
            errors += 1
            continue
        
        year_term = fname_parts[0]
        exam_type = fname_parts[1]
        fileid = fname_parts[2]
        version = fname_parts[-1]
        school_short = "_".join(fname_parts[3:-1])
        
        # OCR 抓 county + 完整學校名
        try:
            pdf_info = extract_school_from_pdf(pdf_path)
            effective_county = pdf_info["county"]
            if effective_county in ("未註明", "其他縣市", UNKNOWN_COUNTY):
                effective_county = None  # → normalize → 其他X (stay)
            effective_school = pdf_info["school_name"]
            if effective_school == "未註明":
                effective_school = school_short
        except Exception:
            effective_county = None
            effective_school = school_short
        
        county_norm = normalize_county(effective_county)
        new_filename = build_archive_filename(
            county=effective_county,
            year_term=year_term,
            exam_type=exam_type,
            fileid=fileid,
            school_name=effective_school,
            version=version,
        )
        
        new_path = (
            ARCHIVE_DIR / county_norm / segment / grade
            / _safe_dirname(subject) / filetype / f"{new_filename}.pdf"
        )
        
        if new_path == pdf_path:
            stayed += 1
            continue
        
        if county_norm == UNKNOWN_COUNTY:
            # 還是 unknown, 不動
            stayed += 1
            continue
        
        try:
            new_path.parent.mkdir(parents=True, exist_ok=True)
            if new_path.exists():
                print(f"  ! skip (target exists): {new_path.relative_to(ARCHIVE_DIR)}")
                # 把 原檔 unlink (因為搬到同檔名)
                pdf_path.unlink()
                stayed += 1
                continue
            shutil.move(str(pdf_path), str(new_path))
            print(f"  ✓ {pdf_path.relative_to(ARCHIVE_DIR)} → {new_path.relative_to(ARCHIVE_DIR)}")
            moved += 1
        except OSError as e:
            print(f"  ✗ error: {pdf_path.relative_to(ARCHIVE_DIR)} → {e}")
            errors += 1
    
    print(f"\nDone:")
    print(f"  Moved (county identified): {moved}")
    print(f"  Stayed (其他X):           {stayed}")
    print(f"  Errors:                   {errors}")


if __name__ == "__main__":
    main()