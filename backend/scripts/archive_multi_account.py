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
import time
from datetime import datetime, timedelta
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


def _check_and_kill_stale_lock(log: logging.Logger):
    """
    如果 lock file 存在且 > 8 分鐘沒更新,代表舊 process 已經 hang/dead。
    Kill 舊 process + 刪 lock file,讓這次 run 能 acquire。

    為什麼需要: 2026-07-22 12:43 發現 archive 從 03:42 卡到 12:29 才被發現,
    原因是 account_status.json 被 race condition mark 成 4 個 exhausted,
    cron 雖然還在跑、但每個 run 都 early exit,lock 一直 hold 住。
    """
    lock_path = _lock_path(ACCOUNT_STATUS_FILE)
    if not lock_path.exists():
        return
    try:
        mtime = lock_path.stat().st_mtime
        age = time.time() - mtime
    except FileNotFoundError:
        return
    if age < 480:  # < 8 分鐘 = 正常
        return
    # Stale!
    log.warning(f"🔍 Stale account_status lock detected (age={age:.0f}s), checking PID...")
    pid_str = "?"
    try:
        pid_str = lock_path.read_text().strip().split("\n")[0]
        pid = int(pid_str)
    except (ValueError, FileNotFoundError, IndexError):
        pid = None
    if pid and pid != os.getpid():
        # Kill old process (SIGTERM first, then SIGKILL if needed)
        try:
            import signal
            os.kill(pid, signal.SIGTERM)
            log.warning(f"  → Sent SIGTERM to PID {pid}")
            time.sleep(2)
            try:
                os.kill(pid, signal.SIGKILL)
                log.warning(f"  → Sent SIGKILL to PID {pid}")
            except ProcessLookupError:
                pass
        except ProcessLookupError:
            log.warning(f"  → PID {pid} already dead")
        except PermissionError:
            log.warning(f"  → No permission to kill PID {pid}")
    # 刪 lock
    try:
        lock_path.unlink()
        log.warning(f"  → Removed stale lock file")
    except FileNotFoundError:
        pass


def _lock_path(target: Path) -> Path:
    """Lock file path next to target (sibling .lock)"""
    return target.with_suffix(target.suffix + ".lock")


def _acquire_lock(target: Path, timeout: float = 60.0, log: logging.Logger | None = None) -> bool:
    """
    取得 account_status.json 的 exclusive lock。

    Race condition 防護:防止兩個 cron run 同時讀寫 status
    - 用 O_CREAT + LOCK_EX
    - 超過 timeout 就當 dead process,強制 steal lock
    """
    import fcntl
    lock_path = _lock_path(target)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            fd = open(lock_path, "w")
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            # 寫入自己的 PID 到 lock file,debug / stale detection 用
            fd.write(f"{os.getpid()}\n")
            fd.flush()
            # 把 fd 留著,後面 release 用 — 但 Python fcntl 在 fd close 時會 release
            # 所以 caller 需要持有 fd 期間都沒問題
            # 解法: 把 fd 存到全域,process 結束自動 release
            _LOCK_FDS[target] = fd
            return True
        except (BlockingIOError, OSError) as e:
            last_err = e
            # 看 lock file 多舊 — stale detection
            try:
                mtime = lock_path.stat().st_mtime
                age = time.time() - mtime
                if age > 480:  # > 8 分鐘 = 死掉的 cron
                    if log:
                        log.warning(f"  🔓 Stale lock detected (age={age:.0f}s), stealing it")
                    try:
                        lock_path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
            except FileNotFoundError:
                pass
            time.sleep(2)
    if log:
        log.warning(f"  ⚠️  Failed to acquire lock after {timeout}s: {last_err}")
    return False


def _release_lock(target: Path):
    """Release lock (close fd → flock 自動 release)"""
    fd = _LOCK_FDS.pop(target, None)
    if fd:
        try:
            fd.close()
        except Exception:
            pass
    # 嘗試刪 lock file (best effort)
    lock_path = _lock_path(target)
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass


# 全域 fd dict: process 持有的 lock fd
_LOCK_FDS: dict[Path, object] = {}


def load_account_status(log: logging.Logger | None = None) -> dict:
    """
    讀 daily account status (race-safe).

    用 fcntl shared lock 防止讀到半寫入狀態。

    【2026-07-22 改】舊的 "exhausted": [...] 欄位會被 drop,改成 "cooldown": {}。
    舊 user 被 mark 為 persistent exhausted (改為今天 00:00 reset 才可重試) 不再有效。
    """
    import fcntl
    if not ACCOUNT_STATUS_FILE.exists():
        return {"date": None, "cooldown": {}}
    # shared lock 讀
    try:
        with open(ACCOUNT_STATUS_FILE, "r") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            try:
                status = json.loads(f.read())
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except (BlockingIOError, OSError):
        # 讀失敗 → fallback 不 lock
        status = json.loads(ACCOUNT_STATUS_FILE.read_text())

    # 【2026-07-22】舊格式轉新格式
    if "exhausted" in status and "cooldown" not in status:
        status["cooldown"] = {}
        status.pop("exhausted", None)

    return status


def save_account_status(status: dict, log: logging.Logger | None = None):
    """
    寫入 daily account status (race-safe + atomic).

    1. 取得 exclusive lock
    2. 寫到 .tmp
    3. atomic rename 到正式 file
    4. release lock
    """
    import fcntl
    ACCOUNT_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)

    # 1. Exclusive lock
    if not _acquire_lock(ACCOUNT_STATUS_FILE, log=log):
        raise RuntimeError("Failed to acquire account_status lock")

    try:
        # 2. Write to tmp
        tmp_path = ACCOUNT_STATUS_FILE.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(status, indent=2, ensure_ascii=False))
        tmp_path.chmod(0o600)

        # 3. Atomic rename
        tmp_path.replace(ACCOUNT_STATUS_FILE)
    finally:
        # 4. Release
        _release_lock(ACCOUNT_STATUS_FILE)


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
    """
    【2026-07-22 改】cooldown-based 而不是 persistent list。
    - `cooldown` 是 dict: account_name -> ISO timestamp when 可重試
    - 過了 cooldown_until 就是「fresh」可重試
    - 沒在 dict 裡也 fresh
    - 跨日自動 reset (date 不同)
    """
    today = datetime.now(TZ_TAIPEI).date().isoformat()
    if status.get("date") != today:
        return False
    cooldown = status.get("cooldown", {})
    until = cooldown.get(account_name)
    if not until:
        return False
    # 過了 cooldown 就 not exhausted
    try:
        cooldown_until = datetime.fromisoformat(until)
        return datetime.now(TZ_TAIPEI) < cooldown_until
    except ValueError:
        return False


def mark_account_exhausted(account_name: str, status: dict, retry_after_minutes: int = 25):
    """
    【2026-07-22 改】記 cooldown_until, 不是 persistent mark。
    原因: StudyArk rate limit 不是永久,是 N 分鐘後可重試。
    """
    today = datetime.now(TZ_TAIPEI).date().isoformat()
    if status.get("date") != today:
        status["date"] = today
        status["cooldown"] = {}
    if "cooldown" not in status:
        status["cooldown"] = {}
    cooldown_until = datetime.now(TZ_TAIPEI) + timedelta(minutes=retry_after_minutes)
    status["cooldown"][account_name] = cooldown_until.isoformat()


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
            # StudyArk search response 用 `download_answer` 標記是否有答案
            # 值: "有" (有) / "无" (無)
            if item.get("download_answer") != "有" and not item.get("daan_url"):
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

    # Stale detection: 如果看到 lock file 過舊 (> 8 分鐘) 代表上次 cron hang 住
    # Kill 舊 process + 刪 lock,讓這次 run 接手
    _check_and_kill_stale_lock(log)

    account_status = load_account_status(log)
    today = datetime.now(TZ_TAIPEI).date().isoformat()
    if account_status.get("date") != today:
        log.info(f"  Reset account_status (new day: {today})")
        account_status = {"date": today, "cooldown": {}}
        save_account_status(account_status)
    elif "exhausted" in account_status:
        # 【2026-07-22 改】舊格式 (exhausted: [...]) 自動轉成新格式 (cooldown: {})
        # 並把轉換後的寫回 disk
        log.info(f"  Migrating old exhausted -> cooldown format")
        account_status["cooldown"] = {}
        account_status.pop("exhausted", None)
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
        # 【2026-07-24 新】log 最早 cooldown_until,讓 user 知道何時可以重試
        cooldowns = account_status.get("cooldown", {})
        if cooldowns:
            from datetime import datetime as _dt
            earliest = None  # tuple (acc_name, until_dt)
            for acc_name, until_str in cooldowns.items():
                try:
                    until = _dt.fromisoformat(until_str)
                    if earliest is None or until < earliest[1]:
                        earliest = (acc_name, until)
                except ValueError:
                    pass
            if earliest:
                acc_name, ts = earliest
                minutes_left = max(0, int((ts - datetime.now(TZ_TAIPEI)).total_seconds() / 60))
                log.warning(f"  All accounts exhausted, exit. Earliest recovery: {acc_name} in {minutes_left}min ({ts.isoformat()})")
            else:
                log.warning("  All accounts exhausted for today, exiting")
        else:
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
    all_exhausted_break = False  # 【2026-07-24 新】一旦所有帳號都 exhausted, break outer batch loop
    for idx, item in enumerate(pending):
        if all_exhausted_break:
            break
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
                log.warning(f"  ✗ {current_account['name']} rate-limited: {e.message[:60]} (cooldown {e.retry_after_minutes}min)")
                mark_account_exhausted(current_account["name"], account_status, retry_after_minutes=e.retry_after_minutes)
                save_account_status(account_status)

                next_acc = get_next_account(accounts, current_account["name"], account_status)
                if next_acc and switch_to_account(next_acc, log):
                    reset_studyark_module()
                    current_account = next_acc
                    log.info(f"  Retry fileid={item.get('id')} with new account")
                    continue
                else:
                    log.warning(f"  All accounts exhausted, skip this fileid + break batch loop")
                    all_exhausted_break = True  # 【2026-07-24 新】跳過剩餘 fileid
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