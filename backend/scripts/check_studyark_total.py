#!/usr/bin/env python3
"""
StudyArk 首頁試卷數量偵測 (2026-07-20 新增)。

每天跑一次:
1. 抓 https://www.studyark.org/ 首頁
2. parse 「XXXX 份中小學試卷」
3. 跟 studyark_total.json 的 last_count 比對
4. 有變化 → 把 delta 加進 archive_status.json.pending_ids 給 archive task 抓
   (實際上 archive task 跑的是 page 1..N, 已經會自動涵蓋新檔案, 這裡只是 log + 監控)

執行:
  cd /home/aping/MyProjects/TQark-web/backend
  uv run python scripts/check_studyark_total.py
"""
import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

BACKEND_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.scraper.archive_path import get_state_path, get_log_path

TZ_TAIPEI = timezone(timedelta(hours=8))
TOTAL_FILE = get_state_path("studyark_total.json")
LOG_FILE = get_log_path("studyark_total.log")
ARCHIVE_STATUS_FILE = get_state_path("archive_status.json")
STUDYARK_HOMEPAGE = "https://www.studyark.org/"


def setup_logging() -> logging.Logger:
    log = logging.getLogger("studyark_total")
    log.setLevel(logging.INFO)
    if log.handlers:
        return log
    fmt = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError:
        pass
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    log.addHandler(ch)
    return log


def load_total_history() -> dict:
    if not TOTAL_FILE.exists():
        return {"history": []}
    try:
        with open(TOTAL_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"history": []}


def save_total_history(data: dict) -> None:
    TOTAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(TOTAL_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


async def fetch_homepage_total(log: logging.Logger) -> int | None:
    """抓 StudyArk 首頁, parse 出試卷總數。"""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
            r = await c.get(
                STUDYARK_HOMEPAGE,
                headers={
                    "User-Agent": "Mozilla/5.0 (TQark-web/0.1 archive-check)",
                },
            )
            if r.status_code != 200:
                log.warning(f"  homepage status {r.status_code}")
                return None
            html = r.text
    except Exception as e:
        log.error(f"  fetch homepage failed: {e}")
        return None

    # 找「XXXX 份中小學試卷」
    # HTML 格式:
    #   <h1>學習方舟 Studyark -<span class="text-primary">20158</span> 份中小學試卷</h1>
    # 數字在 <span> 裡 → 先去掉所有 HTML tags 再 grep
    clean = re.sub(r'<[^>]+>', ' ', html)
    clean = re.sub(r'\s+', ' ', clean)
    m = re.search(r'Studyark\s*-\s*(\d+)\s*份\s*中小學試卷', clean)
    if not m:
        # fallback: 任何「N 份中小學試卷」
        m = re.search(r'(\d+)\s*份\s*中小學試卷', clean)
    if not m:
        log.warning(f"  cannot find 試卷總數 pattern in homepage")
        return None
    return int(m.group(1))


async def main():
    log = setup_logging()
    log.info("=== StudyArk total check start ===")

    total = await fetch_homepage_total(log)
    if total is None:
        log.warning("  沒抓到總數,任務結束")
        return

    history = load_total_history()
    history.setdefault("history", [])

    last_count = None
    if history["history"]:
        last_count = history["history"][-1]["count"]

    log.info(f"  現在總數: {total} (上次: {last_count})")
    if last_count is None:
        delta = 0
    else:
        delta = total - last_count

    if delta > 0:
        log.info(f"  ⚠️ StudyArk 新增了 {delta} 份試卷! archive task 會自動涵蓋這些。")
    elif delta < 0:
        log.warning(f"  ⚠️ StudyArk 試卷總數減少 {abs(delta)} 份(可能被刪除)")
    else:
        log.info(f"  沒有變化")

    history["history"].append({
        "time": datetime.now(TZ_TAIPEI).isoformat(),
        "count": total,
        "delta": delta,
    })
    # 只保留最近 90 天的歷史
    history["history"] = history["history"][-90:]
    history["last_count"] = total
    history["last_check"] = datetime.now(TZ_TAIPEI).isoformat()
    save_total_history(history)
    log.info("=== StudyArk total check done ===")


if __name__ == "__main__":
    asyncio.run(main())
