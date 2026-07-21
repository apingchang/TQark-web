"""
StudyArk Multi-Account Archive Task (簡化版)

跟 archive_task.py 一樣,但是支援多個 StudyArk 帳號輪流:
- 當 rate-limit 觸發,自動切換到下一個帳號 (換 cookies 檔)
- 紀錄每個帳號的 daily counter 狀態
- 一個 batch 內可以跑多個帳號的 quota

邏輯:
1. 從 accounts.json 讀所有 StudyArk 帳號 (cookies 檔 + 名稱)
2. 對每個 batch 內的 fileid:
   - 用目前 active 帳號試下載
   - 如果 rate-limited,標記該帳號為 exhausted,切下一個
   - 重試同一個 fileid,繼續 batch (不 abort 整個 batch)
"""
import asyncio
import json
import logging
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.scraper import studyark
from app.scraper.archive_path import (
    build_archive_path,
    build_archive_filename,
    ensure_archive_dirs,
)
# 用 studyark.StudyArkRateLimit 而不是直接 import, 避免 importlib.reload 後 class identity 改變
StudyArkRateLimit = studyark.StudyArkRateLimit
from app.scraper.archive_path import _safe_dirname
from app.scraper.school_stats import update_school_stats
from app.scraper.pdf_title import extract_school_from_pdf

import zoneinfo

# === Config ===
ARCHIVE_DIR = Path(os.environ.get("TQARK_ARCHIVE_DIR", "/mnt/my_book/考題收集"))
STATE_DIR = ARCHIVE_DIR / "state"
ACCOUNTS_FILE = STATE_DIR / "studyark_accounts.json"
STATUS_FILE = STATE_DIR / "archive_status.json"
ACCOUNT_STATUS_FILE = STATE_DIR / "account_status.json"

INTRA_DELAY = float(os.environ.get("TQARK_ARCHIVE_INTRA_DELAY", "20"))
ITEM_DELAY = float(os.environ.get("TQARK_ARCHIVE_DELAY", "60"))
BATCH_SIZE = int(os.environ.get("TQARK_ARCHIVE_BATCH", "3"))

CREDENTIALS_DIR = Path("/home/aping/MyProjects/TQark-web/credentials")
COOKIES_FILE = CREDENTIALS_DIR / "studyark_cookies.json"

TZ_TAIPEI = zoneinfo.ZoneInfo("Asia/Taipei")


def setup_logging():
    log_file = ARCHIVE_DIR / "logs" / "archive.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(),
        ],
        force=True,
    )
    return logging.getLogger("archive")


def load_accounts() -> list[dict]:
    """從 studyark_accounts.json 讀帳號清單,或用預設 (2 個 fresh 帳號)"""
    if ACCOUNTS_FILE.exists():
        return json.loads(ACCOUNTS_FILE.read_text()).get("accounts", [])
    # 預設: 今天的 fresh 帳號 (ApingChang + 黃瑋婷)
    # 其他 2 個 (WilliamChang / OpenClawChang) 明天 reset 後可加
    return [
        {"name": "ApingChang", "cookies_file": "studyark_cookies_aping.json", "userid": 4172},
        {"name": "黃瑋婷", "cookies_file": "studyark_cookies_hwt.json", "userid": 5845},
    ]


def load_account_status() -> dict:
    """讀 daily account status"""
    if ACCOUNT_STATUS_FILE.exists():
        return json.loads(ACCOUNT_STATUS_FILE.read_text())
    return {"date": None, "exhausted": []}


def save_account_status(status: dict):
    ACCOUNT_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ACCOUNT_STATUS_FILE.write_text(json.dumps(status, indent=2, ensure_ascii=False))


def switch_to_account(account: dict, log: logging.Logger) -> bool:
    """切換 StudyArk cookies 到指定帳號"""
    cookies_file = CREDENTIALS_DIR / account["cookies_file"]
    if not cookies_file.exists():
        log.warning(f"  ⚠️  cookies file not found: {cookies_file}")
        return False
    shutil.copy(cookies_file, COOKIES_FILE)
    COOKIES_FILE.chmod(0o600)
    log.info(f"  🔄 Switched to account: {account['name']} (userid={account.get('userid')})")
    return True


def reset_studyark_module():
    """重置 scraper module (讓 load_cookies() 重新讀檔)"""
    import importlib
    importlib.reload(studyark)
    # 同時重新綁定 StudyArkRateLimit class (避免 except 子句 mismatch)
    global StudyArkRateLimit
    StudyArkRateLimit = studyark.StudyArkRateLimit


def is_account_exhausted(account_name: str, status: dict) -> bool:
    today = datetime.now(TZ_TAIPEI).date().isoformat()
    if status.get("date") != today:
        return False
    return account_name in status.get("exhausted", [])


def mark_account_exhausted(account_name: str, status: dict):
    today = datetime.now(TZ_TAIPEI).date().isoformat()
    if status.get("date") != today:
        status["date"] = today
        status["exhausted"] = []
    if account_name not in status["exhausted"]:
        status["exhausted"].append(account_name)


def get_next_account(accounts: list, current: str | None, status: dict) -> dict | None:
    """找下一個還沒 exhausted 的帳號"""
    for acc in accounts:
        if acc["name"] == current:
            continue
        if not is_account_exhausted(acc["name"], status):
            return acc
    return None


async def fetch_papers_page(page: int) -> dict:
    """抓 StudyArk search API 一頁"""
    raw = await studyark.search_papers(
        grade=None, subject=None, school_year=None, school_term=None,
        exam_type=None, version=None, daan=None, page=page,
    )
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        return {"list": raw, "total": len(raw), "page": page, "total_page": 1}
    return {"list": [], "total": 0, "page": page, "total_page": 0}


async def collect_one(item: dict, account: dict, log: logging.Logger) -> str | None:
    """抓一個 paper (跟 archive_task.py 一樣)"""
    fileid = str(item.get("id"))
    grade = item.get("grade", "")
    subject = item.get("subject", "")
    title = item.get("title", "")
    school_name = item.get("school_name", "")
    classid = str(item.get("classid", ""))
    school_year = item.get("school_year", "")
    school_term = item.get("school_term", "")
    exam_type = item.get("type", "")
    version = item.get("version", "")

    log.info(f"  → [{account['name']}] fileid={fileid} grade={grade} subject={subject} title={title[:30]}...")

    for filetype in ("paper", "daan"):
        if filetype == "daan":
            if not item.get("daan_url") and item.get("daan") != "有":
                continue
        try:
            pdf_bytes, _ = await studyark.download_pdf_stream(
                classid=classid, fileid=fileid, filetype=filetype,
            )
        except StudyArkRateLimit:
            raise  # 往外冒
        except Exception as e:
            log.warning(f"    ✗ {filetype}: error {e}")
            continue

        if not pdf_bytes.startswith(b"%PDF"):
            log.warning(f"    ✗ {filetype}: not a PDF")
            continue

        safe_subject = _safe_dirname(subject or "_未分類")
        year_term = f"{school_year}{school_term}" if school_year else "未分類"
        version_clean = version or "未註明"
        school_clean = school_name or "未註明"
        year_term_safe = year_term.replace("/", "／").replace(":", "：")
        exam_type_safe = (exam_type or "考試").replace("/", "／").replace(":", "：")

        # Step 1: 寫到 tmp file, OCR 抓 county
        # 先用 search response 的 school_name 當 best-guess, OCR 後會 rename
        try:
            tmp_target = build_archive_path(
                grade, subject or "_未分類", filetype,
                f"_tmp_{fileid}_{filetype}",
                county=None,  # unknown county → 其他X folder
            )
            ensure_archive_dirs(grade, subject or "_未分類", filetype, county=None)
            tmp_target.write_bytes(pdf_bytes)
        except OSError as e:
            log.error(f"    ✗ {filetype}: tmp write failed {e}")
            continue
        
        # Step 2: OCR 抓 county + 完整學校名 (從 tmp file)
        try:
            pdf_info = extract_school_from_pdf(tmp_target)
            effective_county = pdf_info["county"]
            if effective_county in ("未註明", "其他縣市"):
                effective_county = None  # → normalize_county → 其他X
            effective_school_name = pdf_info["school_name"]
            if effective_school_name == "未註明":
                effective_school_name = school_clean
        except Exception:
            effective_county = None
            effective_school_name = school_clean
        
        # Step 3: 組正式檔名 + path, 然後從 tmp rename 過去
        filename = build_archive_filename(
            county=effective_county,
            year_term=year_term_safe,
            exam_type=exam_type_safe,
            fileid=fileid,
            school_name=effective_school_name,
            version=version_clean,
        )
        target = build_archive_path(
            grade, subject or "_未分類", filetype, filename,
            county=effective_county,
        )
        if not target:
            try: tmp_target.unlink()
            except OSError: pass
            continue
        
        try:
            ensure_archive_dirs(grade, subject or "_未分類", filetype, county=effective_county)
            # 避免同檔名覆蓋 (rare)
            if target.exists() and target != tmp_target:
                log.warning(f"    ! {filetype}: target already exists, removing tmp {target}")
                try: tmp_target.unlink()
                except OSError: pass
            else:
                tmp_target.rename(target)
            log.info(f"    ✓ {filetype}: {target.relative_to(target.parents[4])} ({len(pdf_bytes)} bytes)")
            
            update_school_stats(
                fileid=fileid,
                school_name=effective_school_name,
                grade=grade,
                subject=subject,
                filetype=filetype,
            )
        except OSError as e:
            log.error(f"    ✗ {filetype}: rename failed {e}")
            try: tmp_target.unlink()
            except OSError: pass
            continue

        if filetype == "paper":
            await asyncio.sleep(INTRA_DELAY)

    return fileid


async def main():
    log = setup_logging()
    log.info(f"=== Multi-account archive task start (batch={BATCH_SIZE}, delay={ITEM_DELAY}s, intra={INTRA_DELAY}s) ===")

    accounts = load_accounts()
    log.info(f"  Loaded {len(accounts)} accounts: {[a['name'] for a in accounts]}")

    if not accounts:
        log.error("No accounts configured")
        return

    account_status = load_account_status()
    today = datetime.now(TZ_TAIPEI).date().isoformat()
    if account_status.get("date") != today:
        log.info(f"  Reset account_status (new day: {today})")
        account_status = {"date": today, "exhausted": []}
        save_account_status(account_status)

    if STATUS_FILE.exists():
        status = json.loads(STATUS_FILE.read_text())
    else:
        status = {"total_collected": 0, "collected_fileids": []}
    collected_set = set(status.get("collected_fileids", []))
    log.info(f"  Already collected {len(collected_set)} fileids")

    # 換到第一個 active 帳號
    current_account = None
    for acc in accounts:
        if not is_account_exhausted(acc["name"], account_status):
            if switch_to_account(acc, log):
                current_account = acc
                reset_studyark_module()
                break

    if not current_account:
        log.warning("  All accounts exhausted for today, exiting")
        return

    # 收集 batch fileids
    pending = []
    page = 1
    while len(pending) < BATCH_SIZE and page <= 200:
        try:
            raw = await fetch_papers_page(page)
        except Exception as e:
            log.error(f"  ✗ fetch page {page} failed: {e}")
            break
        for it in raw.get("list", []):
            fid = str(it.get("id"))
            if fid not in collected_set:
                pending.append(it)
                if len(pending) >= BATCH_SIZE:
                    break
        page += 1
        if page > raw.get("total_page", 1):
            break

    if not pending:
        log.info("  Nothing pending, exit")
        return

    log.info(f"  Will process {len(pending)} pending fileids")

    saved_count = 0
    for idx, item in enumerate(pending):
        if idx > 0:
            log.info(f"  delay {ITEM_DELAY}s before next item...")
            await asyncio.sleep(ITEM_DELAY)

        max_retries = len(accounts)  # 每個 fileid 最多試所有帳號
        for attempt in range(max_retries):
            try:
                fid = await collect_one(item, current_account, log)
                if fid:
                    collected_set.add(fid)
                    saved_count += 1
                break  # 成功,離開 retry loop
            except StudyArkRateLimit as e:
                log.warning(f"  ✗ {current_account['name']} rate-limited: {e.message[:60]}")
                mark_account_exhausted(current_account["name"], account_status)
                save_account_status(account_status)

                next_acc = get_next_account(accounts, current_account["name"], account_status)
                if next_acc and switch_to_account(next_acc, log):
                    reset_studyark_module()
                    current_account = next_acc
                    log.info(f"  Retry fileid={item.get('id')} with new account")
                    continue
                else:
                    log.warning(f"  All accounts exhausted, skip this fileid")
                    break

    status["collected_fileids"] = sorted(collected_set)
    status["total_collected"] = len(collected_set)
    status["last_run"] = datetime.now(TZ_TAIPEI).isoformat()
    status["last_run_result"] = "ok" if saved_count > 0 else "all_failed"
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(json.dumps(status, indent=2, ensure_ascii=False))

    log.info(f"=== Archive task done (saved={saved_count}, total={len(collected_set)}) ===")


if __name__ == "__main__":
    asyncio.run(main())