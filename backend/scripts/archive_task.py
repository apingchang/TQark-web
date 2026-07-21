#!/usr/bin/env python3
"""
考題收集 archive task (2026-07-20 新增)。

每 10 分鐘跑一次:
1. 從 StudyArk API 撈接下來還沒抓的 fileid
2. 一次抓 2 個 (TEST_RATE), 每個間隔 5 秒避免觸發 anti-bot
3. 存到 /mnt/my_book/考題收集/<學段>/<年級>/<科目>/{paper|daan}/<檔名>.pdf
4. 寫 archive_status.json 記錄進度

不抓「下載太頻繁」等 rate-limited response, 直接 abort 整個 batch,
等下次跑再繼續。

執行:
  cd /home/aping/MyProjects/TQark-web/backend
  uv run python scripts/archive_task.py

設定 (透過 env var):
  TQARK_ARCHIVE_DIR         default /mnt/my_book/考題收集
  TQARK_ARCHIVE_BATCH       default 4 (每跑一次抓幾個 fileid)
  TQARK_ARCHIVE_DELAY       default 60 (不同 fileid 之間間隔秒數)
  TQARK_ARCHIVE_INTRA_DELAY default 10 (同一 fileid 內 paper→daan 之間秒數)
"""
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# 讓 import 找得到 app 模組
BACKEND_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.scraper import studyark
from app.scraper.archive_path import (
    build_archive_path, ensure_archive_dirs, get_state_path, get_log_path,
)

# ============================================================
# 設定
# ============================================================
BATCH_SIZE = int(os.environ.get("TQARK_ARCHIVE_BATCH", "4"))
ITEM_DELAY = float(os.environ.get("TQARK_ARCHIVE_DELAY", "60"))
INTRA_DELAY = float(os.environ.get("TQARK_ARCHIVE_INTRA_DELAY", "10"))

STATUS_FILE = get_state_path("archive_status.json")
LOG_FILE = get_log_path("archive.log")

# Taipei 時區
TZ_TAIPEI = timezone(timedelta(hours=8))


# ============================================================
# Logging
# ============================================================
def setup_logging() -> logging.Logger:
    log = logging.getLogger("archive_task")
    log.setLevel(logging.INFO)
    if log.handlers:
        return log

    fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File handler
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError as e:
        print(f"WARN: cannot open log file {LOG_FILE}: {e}", file=sys.stderr)

    # Console handler
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    log.addHandler(ch)

    return log


# ============================================================
# Status file I/O
# ============================================================
def load_status() -> dict:
    if not STATUS_FILE.exists():
        return {
            "last_run": None,
            "last_run_result": None,
            "total_collected": 0,
            "collected_fileids": [],
            "recent_errors": [],
        }
    try:
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {
            "last_run": None,
            "last_run_result": None,
            "total_collected": 0,
            "collected_fileids": [],
            "recent_errors": [],
        }


def save_status(status: dict) -> None:
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)


# ============================================================
# StudyArk 互動
# ============================================================
async def fetch_papers_page(page: int, log: logging.Logger) -> dict:
    """抓 StudyArk search API 一頁, 回傳 dict 含 list / total / page / total_page。"""
    # 用「全部 grade / subject / year」 → 撈到 ID DESC 排序的最新清單
    # 因為 StudyArk 預設 ORDER BY id DESC, 我們 page 1 就拿到最新的
    raw = await studyark.search_papers(
        grade=None,
        subject=None,
        school_year=None,
        school_term=None,
        exam_type=None,
        version=None,
        daan=None,
        page=page,
    )
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        return {"list": raw, "total": len(raw), "page": page, "total_page": 1}
    return {"list": [], "total": 0, "page": page, "total_page": 0}


async def collect_one(item: dict, log: logging.Logger) -> str | None:
    """
    抓一個 StudyArk item (試卷 + 答案) 並存到 archive。
    回傳抓到的 fileid (成功) 或 None (失敗)。
    """
    fileid = str(item.get("id"))
    title = item.get("title", "")
    grade = item.get("grade", "")
    subject = item.get("subject", "")
    school_year = item.get("school_year", "")
    school_term = item.get("school_term", "")
    exam_type = item.get("type", "")
    version = item.get("version", "")
    school_name = item.get("school_name", "")
    classid = str(item.get("classid", ""))

    log.info(f"  → fileid={fileid} grade={grade} subject={subject} title={title[:30]}...")

    # 對 paper 跟 daan 都抓
    for filetype in ("paper", "daan"):
        # 判斷 daan 是否存在 (StudyArk 給的欄位叫 'daan' = '有'/'無')
        # 但 batch archive 直接抓 paper, daan 看 paper response 是否帶 daan_url
        if filetype == "daan":
            # 沒答案就不抓 (StudyArk 欄位叫 'download_answer' = '有'/'无')
            if item.get("download_answer") != "有" and not item.get("daan_url"):
                continue

        try:
            pdf_bytes, _ = await studyark.download_pdf_stream(
                classid=classid, fileid=fileid, filetype=filetype,
            )
        except studyark.StudyArkRateLimit as e:
            log.warning(f"    ✗ {filetype}: rate-limited ({e.message})")
            raise  # 往外冒, abort 整個 batch
        except Exception as e:
            log.warning(f"    ✗ {filetype}: error {e}")
            continue

        # 驗證 PDF magic bytes
        if not pdf_bytes.startswith(b"%PDF"):
            log.warning(f"    ✗ {filetype}: not a PDF (first 30 bytes: {pdf_bytes[:30]!r})")
            continue

        # 組存檔路徑
        from app.scraper.archive_path import _safe_dirname
        safe_subject = _safe_dirname(subject or "_未分類")
        term = school_term or ""
        year_term = f"{school_year}{term}" if school_year else "未分類"
        version_clean = version or "未註明"
        school_clean = school_name or "未註明"
        filename = f"{year_term}_{exam_type or '考試'}_{fileid}_{school_clean}_{version_clean}"

        target = build_archive_path(grade, subject or "_未分類", filetype, filename)
        if not target:
            log.warning(f"    ✗ {filetype}: cannot build path (grade={grade!r}, subject={subject!r})")
            continue

        # ensure dir + 寫入
        try:
            ensure_archive_dirs(grade, subject or "_未分類", filetype)
            target.write_bytes(pdf_bytes)
            log.info(f"    ✓ {filetype}: {target.relative_to(target.parents[3])} ({len(pdf_bytes)} bytes)")
        except OSError as e:
            log.error(f"    ✗ {filetype}: write failed {e}")
            continue

        # 兩個之間 delay 一下避免 burst
        # 【2026-07-21 改】paper → daan 之間 default 10s (原本 1s,衝過頭會撞 StudyArk 限流)
        if filetype == "paper":
            await asyncio.sleep(INTRA_DELAY)

    return fileid


async def main():
    log = setup_logging()
    log.info(f"=== Archive task start (batch={BATCH_SIZE}, delay={ITEM_DELAY}s) ===")

    status = load_status()
    collected_set = set(status.get("collected_fileids", []))
    log.info(f"  已經收過 {len(collected_set)} 個 fileid")

    # 找下一批還沒抓的
    pending = []
    page = 1
    while len(pending) < BATCH_SIZE and page <= 200:  # 200 頁上限,避免無窮迴圈
        try:
            raw = await fetch_papers_page(page, log)
        except Exception as e:
            log.error(f"  ✗ fetch page {page} failed: {e}")
            break

        items = raw.get("list", [])
        if not items:
            log.info(f"  page {page} 已無資料,停止")
            break

        for it in items:
            fid = str(it.get("id"))
            if fid not in collected_set:
                pending.append(it)
                if len(pending) >= BATCH_SIZE:
                    break

        page += 1
        if page > raw.get("total_page", 1):
            break

    if not pending:
        log.info("  沒有 pending 檔案要抓,任務結束")
        status["last_run"] = datetime.now(TZ_TAIPEI).isoformat()
        status["last_run_result"] = "nothing_pending"
        save_status(status)
        return

    log.info(f"  準備抓 {len(pending)} 個新檔案")

    rate_limited = False
    saved_count = 0

    for idx, item in enumerate(pending):
        if idx > 0:
            log.info(f"  delay {ITEM_DELAY}s before next item...")
            await asyncio.sleep(ITEM_DELAY)

        try:
            fid = await collect_one(item, log)
            if fid:
                collected_set.add(fid)
                status["collected_fileids"] = sorted(collected_set)
                status["total_collected"] = len(collected_set)
                saved_count += 1
        except studyark.StudyArkRateLimit as e:
            log.warning(f"  ✗ Rate limited, abort batch: {e.message}")
            rate_limited = True
            status["recent_errors"].append({
                "time": datetime.now(TZ_TAIPEI).isoformat(),
                "error": "rate_limited",
                "detail": e.message,
            })
            # 只保留最近 20 條 error
            status["recent_errors"] = status["recent_errors"][-20:]
            break

    status["last_run"] = datetime.now(TZ_TAIPEI).isoformat()
    status["last_run_result"] = (
        "rate_limited" if rate_limited
        else "ok" if saved_count > 0
        else "all_failed"
    )
    save_status(status)
    log.info(f"=== Archive task done (saved={saved_count}, total={len(collected_set)}) ===")


if __name__ == "__main__":
    asyncio.run(main())
