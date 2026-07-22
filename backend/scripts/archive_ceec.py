"""
CEEC 大學入學考試 archive script。

來源:
- 學測 一般試題 (xsmsid=0J052424829869345634) — 學測 83-115 年
- 學測 特殊試題 (xsmsid=0J052392083839398563)
- 學測 特殊答題卷 (xsmsid=0M111357021798465239)
- 分科 一般試題 (xsmsid=0J052427633128416650) — 指考/分科 83-110 年
- 分科 特殊試題 (xsmsid=0J052424613319003165)
- 分科 特殊答題卷 (xsmsid=0M111360260774151742)

預估: 學測 ~2000 + 分科 ~1300 = ~3300 PDFs

Folder structure:
  /mnt/my_book/考題收集/ceec/
  ├── 學測/
  │   ├── 115年/
  │   │   ├── 115_國綜_試題_01-115學測國綜試題卷.pdf
  │   │   └── ...
  │   └── ...
  ├── 分科/
  │   ├── 115年/
  │   └── ...
  └── 英聽/

【2026-07-22 新增】
"""
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.scraper.ceec import (
    CeecFile,
    download_ceec_pdf,
    list_gsat_files,
)

# Config
ARCHIVE_DIR = Path(os.environ.get("TQARK_ARCHIVE_DIR", "/mnt/my_book/考題收集"))
CEEC_DIR = ARCHIVE_DIR / "ceec"
STATE_DIR = ARCHIVE_DIR / "state"
LOG_DIR = ARCHIVE_DIR / "logs"

CEEC_DIR.mkdir(parents=True, exist_ok=True)
STATE_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

STATUS_FILE = STATE_DIR / "ceec_status.json"


def load_status() -> dict:
    if not STATUS_FILE.exists():
        return {"collected_urls": []}
    return json.loads(STATUS_FILE.read_text())


def save_status(status: dict) -> None:
    tmp = STATUS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(status, indent=2, ensure_ascii=False))
    tmp.replace(STATUS_FILE)


def sanitize_filename(name: str) -> str:
    """處理 Windows / Linux 不允許的字元"""
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)


def build_dest_path(file: CeecFile) -> Path:
    """
    決定 PDF 存哪:
      /mnt/my_book/考題收集/ceec/學測/{year}年/{filename}.pdf
    """
    sub = CEEC_DIR / file.exam_type
    year_str = f"{file.year}年" if file.year else "未分類"
    # 用 title 當檔名 (sanitized)
    name = sanitize_filename(file.title)
    if not name.endswith(".pdf"):
        name += ".pdf"
    return sub / year_str / name


def main():
    print(f"=== CEEC archive task start ===")
    print(f"Archive dir: {CEEC_DIR}")

    status = load_status()
    collected = set(status.get("collected_urls", []))
    print(f"Already collected: {len(collected)} URLs")

    # 收集所有 CEEC PDF
    print("\nFetching file list from www.ceec.edu.tw...")
    all_files = []
    for exam_type in ["學測", "分科"]:
        for category in ["一般", "特殊", "特殊答題卷"]:
            try:
                files = list_gsat_files(exam_type, category, max_pages=25)
                all_files.extend(files)
                print(f"  {exam_type} {category}: {len(files)} PDFs")
            except Exception as e:
                print(f"  ⚠️  {exam_type} {category} 失敗: {e}")

    print(f"\nTotal: {len(all_files)} PDFs")

    saved = 0
    skipped = 0
    failed = 0

    for i, file in enumerate(all_files, 1):
        if file.url in collected:
            skipped += 1
            continue

        dest = build_dest_path(file)
        dest.parent.mkdir(parents=True, exist_ok=True)

        if dest.exists() and dest.stat().st_size > 1000:
            # Already exists, skip download but mark as collected
            with open(dest, "rb") as f:
                if f.read(4) == b"%PDF":
                    collected.add(file.url)
                    skipped += 1
                    continue

        print(f"[{i}/{len(all_files)}] {file.exam_type} {file.year}年 {file.subject} {file.file_type}")
        print(f"  → {dest.name}")

        try:
            actual = download_ceec_pdf(file.url, dest)
            if actual.exists() and actual.stat().st_size > 1000:
                with open(actual, "rb") as f:
                    magic = f.read(4)
                if magic == b"%PDF":
                    collected.add(file.url)
                    saved += 1
                    print(f"  ✓ {actual.stat().st_size:,} bytes")
                else:
                    print(f"  ✗ 不是 PDF (magic: {magic!r})")
                    actual.unlink(missing_ok=True)
                    failed += 1
            else:
                print(f"  ✗ 下載失敗或檔案太小")
                actual.unlink(missing_ok=True)
                failed += 1
        except Exception as e:
            print(f"  ✗ Error: {e}")
            failed += 1

        # CEEC 沒有限流,但禮貌延遲避免太兇
        time.sleep(0.1)

    status["collected_urls"] = sorted(collected)
    status["total_collected"] = len(collected)
    status["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
    status["last_run_result"] = {
        "saved": saved,
        "skipped": skipped,
        "failed": failed,
    }
    save_status(status)

    print(f"\n=== Done ===")
    print(f"  Saved: {saved}")
    print(f"  Skipped: {skipped}")
    print(f"  Failed: {failed}")
    print(f"  Total collected: {len(collected)}")


if __name__ == "__main__":
    main()
