"""
CEEC PDF 重新組織: 把「未分類」folder 的 PDF 根據檔名重新分配到正確年份。

原因: 之前 archive 跑時 ceec.py 的 regex 不夠嚴,很多檔案 year=0 → 都進未分類。
現在 regex 加了指考/sat 支援,可以重新 parse。

【2026-07-22 新增】
"""
import os
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.scraper.ceec import parse_gsat_filename

ARCHIVE_DIR = Path(os.environ.get("TQARK_ARCHIVE_DIR", "/mnt/my_book/考題收集"))
CEEC_DIR = ARCHIVE_DIR / "ceec"


def move_file(pdf: Path) -> tuple[bool, str]:
    """Try to move PDF from 未分類 to correct year folder based on filename."""
    info = parse_gsat_filename(pdf.name)
    if not info["year"]:
        return (False, "no year detected")

    # Decide exam_type based on filename
    if "分科" in pdf.name or "指考" in pdf.name:
        exam_type = "分科"
    elif "學測" in pdf.name or "sat" in pdf.name.lower():
        exam_type = "學測"
    else:
        # Keep current parent (學測 or 分科)
        exam_type = pdf.parent.parent.name  # 學測/未分類 → 學測

    target_dir = CEEC_DIR / exam_type / f"{info['year']}年"
    target_path = target_dir / pdf.name

    if target_path.exists():
        # Already at correct location (or collision), just remove the original
        pdf.unlink()
        return (True, f"merged: {target_path}")

    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(pdf), str(target_path))
    return (True, f"moved to {info['year']}年")


def main():
    print(f"=== CEEC reorganize task ===")
    print(f"Looking for 未分類 PDFs...")

    moved = 0
    skipped = 0
    failed = 0

    for exam_type in ["學測", "分科"]:
        unknown_dir = CEEC_DIR / exam_type / "未分類"
        if not unknown_dir.exists():
            continue

        for pdf in unknown_dir.glob("*.pdf"):
            ok, msg = move_file(pdf)
            if ok:
                moved += 1
                if moved <= 5:
                    print(f"  ✓ {pdf.name[:50]} → {msg}")
            else:
                skipped += 1

    # Remove empty 未分類 folders
    for exam_type in ["學測", "分科"]:
        unknown_dir = CEEC_DIR / exam_type / "未分類"
        if unknown_dir.exists():
            contents = list(unknown_dir.iterdir())
            if not contents:
                unknown_dir.rmdir()
                print(f"  Removed empty: {unknown_dir}")

    print(f"\n=== Done ===")
    print(f"  Moved: {moved}")
    print(f"  Skipped (no year): {skipped}")
    print(f"  Failed: {failed}")
    print(f"  Total files: {sum(1 for _ in CEEC_DIR.rglob('*.pdf'))}")


if __name__ == "__main__":
    main()
