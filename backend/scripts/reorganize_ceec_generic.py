#!/usr/bin/env python3
"""
CEEC generic 檔搬移: 把「未分類」folder 的 PDF 統一搬到 ceec/_generic/。

【2026-07-24 William 要求】
原因: 學測/未分類/ 有 6 個 generic instruction files (e.g. "使用a3版面特殊答題卷.pdf"),
     分科/未分類/ 有 32 個 "月-編號" 格式檔 (e.g. "180-5-01國文.pdf"),
     這 38 個都沒年份資訊,regex 抓不到,放「未分類」讓 UI 看起來沒整理好。

新位置: ceec/_generic/ 統一資料夾 (下劃線前綴避免跟 exam_type "學測"/"分科" 衝突)

注意:
- _scan_pdf_tree 用 rel.parts[0] 當 exam_type, _generic 會被當作一個新 exam_type
- 但 _scan_pdf_tree 對 parts[0] 不做過濾, 所以 _generic 裡的檔也會被掃到
- web UI 顯示所有 exam_types → 「_generic」會出現在 filter 列表
  - 解法: 把 _generic 也過濾掉 (或重新命名成 hidden-prefix)
- 更好: 用 `_GENERIC` 或加 SKIP list

執行:
  TQARK_ARCHIVE_DIR=/mnt/my_book/考題收集 uv run python scripts/reorganize_ceec_generic.py
"""
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

ARCHIVE_DIR = Path(os.environ.get("TQARK_ARCHIVE_DIR", "/mnt/my_book/考題收集"))
CEEC_DIR = ARCHIVE_DIR / "ceec"
GENERIC_DIR = CEEC_DIR / "_generic"

# Skip from scan (prefix: "_" 是 OpenClaw 慣例的 hidden 標記)
GENERIC_PREFIX = "_generic"


def main():
    print(f"=== CEEC generic 檔搬移 task ===")
    print(f"Source: {CEEC_DIR}/<學測|分科>/未分類/")
    print(f"Target: {GENERIC_DIR}/")
    print()

    if not CEEC_DIR.exists():
        print(f"❌ CEEC_DIR not found: {CEEC_DIR}")
        return

    GENERIC_DIR.mkdir(parents=True, exist_ok=True)

    moved = 0
    skipped = 0
    by_exam = {"學測": 0, "分科": 0}

    for exam_type in ["學測", "分科"]:
        unknown_dir = CEEC_DIR / exam_type / "未分類"
        if not unknown_dir.exists():
            continue

        for pdf in sorted(unknown_dir.glob("*.pdf")):
            target = GENERIC_DIR / pdf.name
            if target.exists():
                # 名稱碰撞 → 加 exam_type 前綴區分
                target = GENERIC_DIR / f"{exam_type}_{pdf.name}"

            shutil.move(str(pdf), str(target))
            moved += 1
            by_exam[exam_type] += 1
            if moved <= 8:
                print(f"  ✓ {exam_type}/未分類/{pdf.name[:40]} → _generic/")
            elif moved == 9:
                print(f"  ... (skip showing more)")

    # Remove empty 未分類 folders
    removed_dirs = []
    for exam_type in ["學測", "分科"]:
        unknown_dir = CEEC_DIR / exam_type / "未分類"
        if unknown_dir.exists():
            contents = list(unknown_dir.iterdir())
            if not contents:
                unknown_dir.rmdir()
                removed_dirs.append(unknown_dir)
                print(f"  Removed empty folder: {unknown_dir.relative_to(ARCHIVE_DIR)}")

    print(f"\n=== Done ===")
    print(f"  Moved: {moved}")
    print(f"  By exam type: 學測={by_exam['學測']}, 分科={by_exam['分科']}")
    print(f"  Empty folders removed: {len(removed_dirs)}")
    print(f"  Total files in CEEC: {sum(1 for _ in CEEC_DIR.rglob('*.pdf'))}")
    print(f"  Total files in _generic/: {sum(1 for _ in GENERIC_DIR.glob('*.pdf'))}")


if __name__ == "__main__":
    main()