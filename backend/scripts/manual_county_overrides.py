#!/usr/bin/env python3
"""手動標記 其他X PDF 的 county。

用法: 編輯 SCHOOL_TO_COUNTY dict, 然後執行這個 script。
script 會:
1. 讀取其他X/ 內所有 PDF
2. 從 filename 抽出 school_short (e.g. 縣立伸仁國小 → 伸仁國小)
3. 對照 SCHOOL_TO_COUNTY
4. 移動 PDF 到正確 county folder

範例:
    SCHOOL_TO_COUNTY = {
        "伸仁國小": "彰化縣",
        "三潭國小": "彰化縣",
        ...
    }
"""

import os
import shutil
import re
from pathlib import Path
import sys
sys.path.insert(0, ".")

# === 編輯這個 dict 來標記 county ===
# key = school short name (從 filename 抽出)
# value = county
SCHOOL_TO_COUNTY = {
    # 國小
    "縣立伸仁國小": "彰化縣",  # 彰化縣伸港鄉
    "縣立三潭國小": "彰化縣",  # 彰化縣二林鎮
    "縣立大莊國小": "彰化縣",
    "縣立舊館國小": "彰化縣",
    "縣立大園國小": "桃園市",  # 桃園市大園區 (但用縣立)
    "市立興南國小": "臺南市",  # 臺南市南區
    "市立成德國小": "臺北市",  # 臺北市南港區 (但前面會自動抓到)
    "市立四維國小": "新北市",
    "市立文心國小": "臺中市",
    
    # 國中
    "縣立埔心國中": "彰化縣",  # 彰化縣埔心鄉
    "市立北興國中": "嘉義市",
    "市立三民高中附設國中": "桃園市",
    "市立文昌國中": "新北市",  # 鶯歌區?  三民區?
}
# ======================================

ARCHIVE_ROOT = Path("/mnt/my_book/考題收集")

def extract_school_short(filename: str) -> str:
    """從 filename 抽出 school_short, e.g. 其他X_108_期中考_35685_縣立埔心國中_康軒.pdf → 縣立埔心國中"""
    # 格式: 其他X_<year_term>_<fileid>_<school>_<version>.pdf
    parts = filename.replace(".pdf", "").split("_")
    # school 在倒數第 2 個 (倒數第 1 是 version)
    if len(parts) >= 4:
        return parts[-2]
    return ""


def find_other_x_pdfs():
    other_x_dir = ARCHIVE_ROOT / "其他X"
    if not other_x_dir.exists():
        return []
    return sorted(other_x_dir.rglob("*.pdf"))


def move_to_county(pdf_path: Path, county: str):
    """移動 PDF 從 其他X/<path> → <county>/<path>"""
    relative = pdf_path.relative_to(ARCHIVE_ROOT / "其他X")
    target = ARCHIVE_ROOT / county / relative
    
    # 重建 filename with county prefix
    # 其他X_xxx_yyy_zzz_school_ver.pdf → <county>_xxx_yyy_zzz_school_ver.pdf
    parts = pdf_path.stem.split("_")
    if parts[0] == "其他X":
        parts[0] = county
    new_stem = "_".join(parts)
    new_target = target.parent / f"{new_stem}.pdf"
    
    target.parent.mkdir(parents=True, exist_ok=True)
    print(f"  {pdf_path.relative_to(ARCHIVE_ROOT)}")
    print(f"    → {new_target.relative_to(ARCHIVE_ROOT)}")
    shutil.move(str(pdf_path), str(new_target))


def main():
    pdfs = find_other_x_pdfs()
    print(f"Found {len(pdfs)} PDFs in 其他X/\n")
    
    moved = 0
    skipped = 0
    for pdf in pdfs:
        school_short = extract_school_short(pdf.name)
        if not school_short:
            print(f"  ? {pdf.name}: can't parse school")
            skipped += 1
            continue
        
        if school_short in SCHOOL_TO_COUNTY:
            county = SCHOOL_TO_COUNTY[school_short]
            print(f"\n✓ {pdf.name[:60]} → {county}")
            move_to_county(pdf, county)
            moved += 1
        else:
            print(f"⊝ {pdf.name[:60]} (no override)")
            skipped += 1
    
    print(f"\n=== Moved: {moved}, Skipped: {skipped} ===")


if __name__ == "__main__":
    main()
