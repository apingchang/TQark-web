"""
CAP 會考 archive script (一次跑抓全部 ~290 PDFs)。

公開資源、免登入、無限流,所以可以一次跑完。

Folder structure:
  /mnt/my_book/考題收集/cap_exam/
  ├── 會考/
  │   ├── 115年/
  │   │   ├── 01-115學測國綜試題.pdf
  │   │   └── ...
  │   ├── 114年/
  │   └── ...
  └── 基測/
      ├── 83年/
      └── ...

【2026-07-22 新增】
"""
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.scraper.cap_exam import (
    CapExamFile,
    download_gdrive,
    list_all_cap_files,
)

# Config
ARCHIVE_DIR = Path(os.environ.get("TQARK_ARCHIVE_DIR", "/mnt/my_book/考題收集"))
CAP_DIR = ARCHIVE_DIR / "cap_exam"
STATE_DIR = ARCHIVE_DIR / "state"
LOG_DIR = ARCHIVE_DIR / "logs"

CAP_DIR.mkdir(parents=True, exist_ok=True)
STATE_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

STATUS_FILE = STATE_DIR / "cap_exam_status.json"


def load_status() -> dict:
    if not STATUS_FILE.exists():
        return {"collected_fileids": []}
    return json.loads(STATUS_FILE.read_text())


def save_status(status: dict) -> None:
    tmp = STATUS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(status, indent=2, ensure_ascii=False))
    tmp.replace(STATUS_FILE)


def build_dest_path(file: CapExamFile) -> Path:
    """
    決定 PDF 存哪:
      /mnt/my_book/考題收集/cap_exam/會考/{year}年/{clean_title}.pdf
    """
    if file.exam_type == "基測":
        sub = CAP_DIR / "基測"
        year_str = f"{file.year}年" if file.year else "未分類"
    else:
        sub = CAP_DIR / "會考"
        year_str = f"{file.year}年"

    # 從 title 抽出檔名, 避免同名撞檔
    title = file.title.strip()
    # 用 subject + year + short file_id 確保唯一
    clean = f"{file.year}_{file.subject}_{file.file_id[:8]}"
    if title and title != file.subject:
        # 把 title 也加進去 (讓人看得懂)
        # 但限制長度避免路徑過長
        title_clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', title)
        title_clean = title_clean[:30]
        clean = f"{file.year}_{file.subject}_{title_clean}_{file.file_id[:6]}"

    if not clean.endswith(".pdf"):
        clean += ".pdf"

    return sub / year_str / clean


def sanitize_filename(name: str) -> str:
    """處理 Windows / Linux 不允許的字元"""
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)





def classify_priority(file: CapExamFile) -> int:
    """
    分優先級: 越低越優先下載。

    1 = 核心試題 (試題本/參考答案/評分原則) — user 主要想看的
    2 = 樣卷/答題卷/寫作樣本
    3 = 統計表/各題通過率/鑑別度/其他報告
    """
    title = file.title
    subject = file.subject

    # 優先 1: 核心
    if subject in {"國文", "英語", "數學", "社會", "自然", "寫作測驗", "參考答案", "試題說明"}:
        return 1
    # 優先 2: 樣卷/答題卷
    if "樣卷" in title or "答題卷" in title or "級分" in title:
        return 2
    # 優先 3: 其他
    return 3


def download_one(file: CapExamFile) -> tuple[str, bool, str]:
    """下載單一 PDF, 回傳 (file_id, success, message)"""
    dest = build_dest_path(file)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest = dest.parent / sanitize_filename(dest.name)

    if dest.exists() and dest.stat().st_size > 1000:
        with open(dest, "rb") as f:
            if f.read(4) == b"%PDF":
                return (file.file_id, True, "already exists")

    try:
        actual = download_gdrive(file.file_id, dest, quiet=True)
        if actual.exists() and actual.stat().st_size > 1000:
            with open(actual, "rb") as f:
                magic = f.read(4)
            if magic == b"%PDF":
                return (file.file_id, True, f"{actual.stat().st_size:,} bytes")
        actual.unlink(missing_ok=True)
        return (file.file_id, False, "invalid file")
    except Exception as e:
        return (file.file_id, False, str(e)[:60])



def main():
    print(f"=== CAP exam archive task start ===")
    print(f"Archive dir: {CAP_DIR}")

    status = load_status()
    collected = set(status.get("collected_fileids", []))
    print(f"Already collected: {len(collected)} PDFs")

    print("\nFetching file list from cap.rcpet.edu.tw...")
    files = list_all_cap_files()
    print(f"Found {len(files)} PDFs total")

    saved = 0
    skipped = 0
    failed = 0

    # Sort by priority (核心試題優先)
    files_with_priority = [(classify_priority(f), f) for f in files]
    files_with_priority.sort(key=lambda x: (x[0], x[1].year))
    files = [f for _, f in files_with_priority]

    # Filter out already-collected
    pending = [f for f in files if f.file_id not in collected]
    print(f"\n待下載: {len(pending)} PDFs (排除已收集 {len(files) - len(pending)} 個)")

    # Parallel download with ThreadPoolExecutor
    MAX_WORKERS = 4  # Google Drive 直接下載,可以平行
    print(f"使用 {MAX_WORKERS} workers 並行下載...")

    completed = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_file = {executor.submit(download_one, f): f for f in pending}
        for future in as_completed(future_to_file):
            file = future_to_file[future]
            try:
                file_id, success, msg = future.result()
            except Exception as e:
                file_id, success, msg = file.file_id, False, str(e)[:60]

            completed += 1
            if success:
                if msg == "already exists":
                    skipped += 1
                else:
                    collected.add(file_id)
                    saved += 1
                    print(f"[{completed}/{len(pending)}] ✓ {file.year}年 {file.subject} {file.title[:30]} - {msg}")
            else:
                failed += 1
                print(f"[{completed}/{len(pending)}] ✗ {file.year}年 {file.subject} {file.title[:30]} - {msg}")

            # 定期 save status (避免大量 work 丟失)
            if completed % 20 == 0:
                status["collected_fileids"] = sorted(collected)
                status["total_collected"] = len(collected)
                save_status(status)

    # Save status
    status["collected_fileids"] = sorted(collected)
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
    print(f"  Skipped (already have): {skipped}")
    print(f"  Failed: {failed}")
    print(f"  Total collected: {len(collected)}")


if __name__ == "__main__":
    main()
