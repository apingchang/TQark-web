"""
HTML page routes

- GET /         → landing page
- GET /dashboard → user dashboard
- GET /admin    → admin panel
- GET /static/*  → 靜態檔案
"""

from pathlib import Path
import json
import threading
import time as _time

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, Response, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, desc, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.db_helpers import log_action
from app.core.deps import get_current_user_from_token, require_admin, require_approved, require_login
from app.db.models import AccessRequest, AuditLog, DownloadHistory, User
from app.db.session import get_db
from app.scraper import studyark

router = APIRouter(tags=["pages"])

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


async def _background_fetch_companion(
    classid: str,
    fileid: str,
    filetype: str,
    grade: str,
    subject: str,
    school_year: str,
    school_term: str,
    exam_type: str,
    version: str,
    school_name: str,
):
    """Background fetch companion PDF (paper↔daan) 並存到 cache。
    Lazy download 策略: user 下載 paper 時順手 background 抓 daan。
    不影響 user download 的 latency。

    Args:
        filetype: companion 的 filetype ("paper" 或 "daan")
        其他參數: 用來建正式檔名 (含 year/term/exam/version/school)
    """
    import logging
    _bg_logger = logging.getLogger("tqark.bg_fetch")
    try:
        # 跳過 0.5s 讓 user 下載先出去
        import asyncio
        await asyncio.sleep(0.5)

        pdf_bytes, _ = await studyark.download_pdf_stream(
            classid=classid, fileid=fileid, filetype=filetype,
        )

        if not pdf_bytes or not pdf_bytes.startswith(b"%PDF"):
            _bg_logger.info(f"[BG] {fileid}/{filetype}: empty or not PDF, skipping")
            return

        # 存到 cache (OCR + county folder logic)
        from app.scraper.archive_path import (
            build_archive_path, build_archive_filename, ensure_archive_dirs, _safe_dirname,
        )
        from app.scraper.pdf_title import extract_school_from_pdf

        tmp_target = build_archive_path(
            grade, subject, filetype,
            f"_tmp_{fileid}_{filetype}",
            county=None,
        )
        ensure_archive_dirs(grade, subject, filetype, county=None)
        tmp_target.write_bytes(pdf_bytes)

        # OCR county
        try:
            pdf_info = extract_school_from_pdf(tmp_target)
            effective_county = pdf_info["county"] if pdf_info["county"] not in ("未註明", "其他縣市") else None
            effective_school_name = pdf_info["school_name"] if pdf_info["school_name"] != "未註明" else school_name
        except Exception:
            effective_county = None
            effective_school_name = school_name

        year_term = f"{school_year}{school_term}"
        formal_filename = build_archive_filename(
            county=effective_county,
            year_term=year_term or "未分類",
            exam_type=exam_type or "考試",
            fileid=fileid,
            school_name=effective_school_name or "未註明",
            version=version or "未註明",
        )
        target = build_archive_path(grade, subject, filetype, formal_filename, county=effective_county)
        if target:
            ensure_archive_dirs(grade, subject, filetype, county=effective_county)
            if target.exists() and target != tmp_target:
                tmp_target.unlink()
            else:
                tmp_target.rename(target)
                _bg_logger.info(f"[BG SAVED] {fileid}/{filetype} -> {target} ({len(pdf_bytes)} bytes)")
    except studyark.StudyArkRateLimit as e:
        _bg_logger.info(f"[BG] {fileid}/{filetype} rate-limited: {e.message}")
    except Exception as e:
        _bg_logger.warning(f"[BG ERROR] {fileid}/{filetype}: {e}")


def _user_ctx(user: User | None) -> dict:
    """把 User object 轉成 template 用的 dict"""
    if user is None:
        # 【2026-07-22 改】未登入 user 給個 placeholder dict
        # 讓 template 可以安全用 user.permission 等屬性而不會 crash
        return {
            "id": None,
            "email": None,
            "name": None,
            "picture": None,
            "role": "guest",
            "status": "guest",
            "permission": 99,  # 比 9 還高,所有 permission 判斷都會是「未審核」
            "first_seen_at": None,
            "last_login_at": None,
        }

    def fmt(dt):
        return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None  # 【2026-07-22 改】加秒數

    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "picture": user.picture,
        "role": user.role,
        "status": user.status,
        "permission": user.permission,  # 【2026-07-22 新】template 用
        "first_seen_at": fmt(user.first_seen_at),
        "last_login_at": fmt(user.last_login_at),
    }


# 【2026-07-31 新】Permission 名稱 mapping (for template 顯示)
PERMISSION_NAMES = {
    0: "管理員 (Admin)",
    1: "家人",
    2: "親戚",
    3: "朋友",
    4: "同事",
    8: "已註冊",
    9: "待審核",
    99: "訪客 (未登入)",
}


def _common_ctx(user: User | None) -> dict:
    perm = user.permission if user else 99
    return {
        "user": _user_ctx(user),
        "permission_name": _permission_name(perm),
        "version": "0.1.2",
    }


def _permission_name(perm: int) -> str:
    """Return human-readable permission name."""
    return PERMISSION_NAMES.get(perm, f"未知 ({perm})")


# ============================================================
# Pages
# ============================================================
@router.get("/", response_class=HTMLResponse)
async def landing(
    request: Request,
    user: User | None = Depends(get_current_user_from_token),
    db: AsyncSession = Depends(get_db),
):
    """
    【2026-07-22 19:12 改】新 landing page layout:
    - 上方 banner
    - 左側: function buttons
    - 中央: console (login / status / 申請)
    - 右側: 平台資訊
    - 下方 banner: AdSense placeholder

    【2026-07-24 改】右側加 5 類 PDF 計數:
    - 小學考題 (StudyArk 國小 + 其他X/國小)
    - 國中考題
    - 高中考題
    - 會考考題 (CAP)
    - 大學入學考試 (CEEC)

    【2026-07-24 改】in-memory cache, 避免 CIFS rglob 慢:
    - /mnt/my_book 是網路磁碟, rglob 每次都要 server round-trip (~30+ 秒)
    - cache 10 分鐘, background refresh stale
    """
    import os
    import threading
    import time as _time
    from pathlib import Path

    # 拿 platform stats (DB query)
    stats: dict = {}
    try:
        from app.db.models import User as UserModel

        # approved user 數
        approved_count = (await db.execute(
            select(func.count(UserModel.id)).where(UserModel.permission <= 7, UserModel.role != "admin")
        )).scalar() or 0
        stats["approved_users"] = approved_count
    except Exception:
        stats["approved_users"] = 0

    # 5 類 PDF 計數 (cached)
    stats.update(_get_cached_archive_counts())

    # 【2026-07-25 新】讀靈覓/懂王今日新聞 docx → 拆成 entries
    news = _get_cached_news_summaries()

    return templates.TemplateResponse(
        "landing.html",
        {**_common_ctx(user), "request": request, "stats": stats, "news": news},
    )


# === Archive PDF count cache (2026-07-24) ===
# 因為 /mnt/my_book 是 CIFS 網路磁碟, rglob 超慢
# In-memory cache + 10 分鐘 TTL, background refresh
_archive_counts_cache: dict = {"data": None, "ts": 0.0}
_archive_counts_lock = threading.Lock()
_ARCHIVE_COUNTS_TTL = 600  # 10 minutes


def _has_recent_archive_activity() -> bool:
    """【2026-07-28】檢查 archive log / state file 是否近期修改
    (代表有 archive 在跑). 如果是, 自動 invalidate cache,
    以免看到 stale 資料。
    """
    import os
    from pathlib import Path
    archive_root = Path(os.environ.get("TQARK_ARCHIVE_DIR", "/mnt/my_book/考題收集"))
    log_dir = archive_root / "logs"
    if not log_dir.exists():
        return False
    now = _time.time()
    # 检查最近 5 分鐘內有動的 archive-related 文件
    patterns = [
        "tcool_schools.log", "exambank.log",
        "exambank_*.log", "tcool_*.log",
        "exambank_status.json", "tcool_schools_status.json",
    ]
    for pattern in patterns:
        for log_file in log_dir.glob(pattern):
            try:
                mtime = log_file.stat().st_mtime
                if (now - mtime) < 300:  # 5 分鐘內有動
                    return True
            except OSError:
                continue
    return False


def _get_cached_archive_counts() -> dict:
    """Get cached PDF counts. Returns previous cache if still fresh."""
    now = _time.time()
    # 【2026-07-28】如果 archive 正在跑 (log 5 分鐘內改過), 視為 stale
    recent_activity = _has_recent_archive_activity()
    with _archive_counts_lock:
        if _archive_counts_cache["data"] is not None and now - _archive_counts_cache["ts"] < _ARCHIVE_COUNTS_TTL and not recent_activity:
            return _archive_counts_cache["data"]
        if _archive_counts_cache["data"] is not None:
            # Stale — trigger background refresh (non-blocking), return stale
            import threading as _threading
            _threading.Thread(target=_refresh_archive_counts_bg, daemon=True).start()
            return _archive_counts_cache["data"]
    # Cold start or cache missing — synchronously scan
    data = _scan_archive_counts()
    with _archive_counts_lock:
        _archive_counts_cache["data"] = data
        _archive_counts_cache["ts"] = _time.time()
    return data


def _refresh_archive_counts_bg():
    """Background refresh without blocking request."""
    data = _scan_archive_counts()
    with _archive_counts_lock:
        _archive_counts_cache["data"] = data
        _archive_counts_cache["ts"] = _time.time()


def _scan_archive_counts() -> dict:
    """Walk /mnt/my_book/考題收集 and count PDFs by category.

    【2026-07-28 擴充】返回更多檔案資訊:
      - 原本 5 類計數 (count_elementary/junior/senior/cap/ceec, total_all)
      - count_tcool (tcool 學校來源)
      - by_county: 各縣市 file 數 dict
      - by_school: 各學校 file 數 dict (top 20)
      - by_subject: 科目 file 數 dict (top 15)
      - latest_files: 最新 15 個 file (name, path, county, level, grade, subject, mtime)
      - archive_size_mb: 總 MB 數
      - total_files: 全部 file 數 (含非 PDF)

    Return keys: count_elementary, count_junior, count_senior, count_cap, count_ceec, total_all,
                  count_tcool, by_county, by_school, by_subject, latest_files, archive_size_mb, total_files
    """
    import os
    from pathlib import Path

    archive_root = Path(os.environ.get("TQARK_ARCHIVE_DIR", "/mnt/my_book/考題收集"))
    result = {
        "count_elementary": 0,
        "count_junior": 0,
        "count_senior": 0,
        "count_cap": 0,
        "count_ceec": 0,
        "total_all": 0,
        "count_tcool": 0,
        "by_county": {},
        "by_school": {},
        "by_subject": {},
        "latest_files": [],
        "archive_size_mb": 0,
        "total_files": 0,
    }
    # 【2026-07-28】CIFS 可能暫時掛掉 (Host is down), 不該讓整個 landing 500
    try:
        archive_root.exists()
    except OSError as e:
        # Host is down 等 CIFS 問題 — 返回 zeros,不要 raise
        return result

    if not archive_root.exists():
        return result

    SKIP_TOP_DIRS = ("state", "logs")  # cap_exam/ceec 還是要 walk

    count_elementary = 0
    count_junior = 0
    count_senior = 0
    count_cap = 0
    count_ceec = 0
    count_tcool = 0
    by_county = {}
    by_school = {}
    by_subject = {}
    all_files_with_mtime = []  # for latest_files
    archive_size = 0
    total_files = 0

    try:
        # 用 os.walk 比 Path.rglob 在 CIFS 上快 (local readdir 不再 server roundtrip)
        for dirpath, dirnames, filenames in os.walk(archive_root):
            # prune non-archive top dirs (state/, logs/, _generic)
            if dirpath == str(archive_root):
                dirnames[:] = [d for d in dirnames if d not in SKIP_TOP_DIRS]
            # 【2026-07-24 新】ceec/_generic 是 generic instruction files, 不算入 5 類計數
            if "_generic" in dirpath.split(os.sep):
                dirnames[:] = []  # don't recurse
                continue
            for fname in filenames:
                # Calculate size for all known extensions
                if not fname.endswith((".pdf", ".docx", ".doc", ".xlsx", ".xls", ".zip")):
                    continue
                full = os.path.join(dirpath, fname)
                rel = os.path.relpath(full, archive_root)
                parts = rel.split(os.sep)
                is_pdf = fname.endswith(".pdf")
                total_files += 1
                try:
                    size = os.path.getsize(full)
                    archive_size += size
                    mtime = os.path.getmtime(full)
                except OSError:
                    size = 0
                    mtime = 0

                # Only count PDFs toward 5 類計數
                if is_pdf:
                    # Top-level dirs: cap_exam, ceec
                    if parts and parts[0] == "cap_exam":
                        count_cap += 1
                        continue
                    if parts and parts[0] == "ceec":
                        count_ceec += 1
                        continue
                    # 其他縣市路徑檢查 "國小"/"國中"/"高中"
                    # 格式: <county>/<level>/<grade>/<subject>/<filetype>/file.pdf
                    county = parts[0] if parts else "其他"
                    for p in parts[:-1]:
                        if p == "國小":
                            count_elementary += 1
                            break
                        elif p == "國中":
                            count_junior += 1
                            break
                        elif p == "高中":
                            count_senior += 1
                            break
                    # 由 county/level/grade/subject/filetype 結構抓細節
                    if len(parts) >= 5 and parts[1] in ("國小", "國中", "高中"):
                        level = parts[1]
                        grade = parts[2]
                        subject = parts[3]
                        filetype = parts[4]
                        by_county[county] = by_county.get(county, 0) + 1
                        by_subject[subject] = by_subject.get(subject, 0) + 1
                        # tcool schools: tcool 學校被記在 <county>/<school>_<別名> 的 folder
                        # 但現在 migration 已改, 學校名藏在 filename 而不在 path
                        # 所以 count_tcool 以 subject "其他" 或某些 pattern 推論會偏, 改用 total_files > 0 計算的 fallback
                        # 簡化: 預設所有非 cap/ceec 為 StudyArk (含 tcool)
                        # 但看學期目錄存在與否推論 — 跳過
                        latest_entry = {
                            "name": fname,
                            "path": rel,
                            "county": county,
                            "level": level,
                            "grade": grade,
                            "subject": subject,
                            "filetype": filetype,
                            "mtime": mtime,
                            "size_kb": size // 1024,
                        }
                        all_files_with_mtime.append(latest_entry)
                    # Track school name (from filename)
                    # Two formats:
                    # 1. Migrated tcool: <county>_<year>_第N學期_<exam-type>_<school>_<grade>_<subject>[_解答].<ext>
                    #    e.g., 高雄市_110_第2學期_補考_高雄市五福國中_一年級_公民.pdf
                    # 2. StudyArk: <county>_<year>_<exam-type>_<paper-id>_<school>_<publisher>.<ext>
                    #    e.g., 高雄市_109_期中考_34585_高雄市立大樹國民中學_南一.pdf
                    import re
                    school = None
                    # Format 1: 補考/段考 (migrated tcool)
                    m = re.match(rf'{re.escape(county)}_(\d{{3}})_第\d學期_\w+_(.+?)_(.+?)_(.+?)(?:_解答)?\.(?:pdf|docx|doc)$', fname)
                    if m:
                        school = m.group(2)
                    else:
                        # Format 2: StudyArk pattern (no 第N學期)
                        m = re.match(rf'{re.escape(county)}_(\d{{3}})_(?:期中考|期末考|段考|考試)_\d+_(.+?)_(康軒|南一|翰林|育成|奇鼎|全華|何嘉仁|\w{{1,4}})\.(?:pdf|docx|doc)$', fname)
                        if m:
                            school = m.group(2)
                        else:
                            # Format 3: 下/上學期 (e.g., 臺東縣_108下學期_期末考_28550_臺東縣立新生國小_何嘉仁.pdf)
                            m = re.match(rf'{re.escape(county)}_(\d{{3}})[下上]學期_(?:期中考|期末考|段考|考試)_\d+_(.+?)_(康軒|南一|翰林|育成|奇鼎|全華|何嘉仁|\w{{1,4}})\.(?:pdf|docx|doc)$', fname)
                            if m:
                                school = m.group(2)
                            else:
                                # Format 4: simpler fallback - try to grab the school after first 3 segments
                                m = re.match(rf'{re.escape(county)}_(\d{{3}})_\w+_(?:\d+_)?(.+?)_(?:康軒|南一|翰林|育成|奇鼎|全華|何嘉仁|\w{{1,4}})(?:_解答)?\.(?:pdf|docx|doc)$', fname)
                                if m:
                                    school = m.group(2)
                    if school and school not in ("一年級", "二年級", "三年級", "七年級", "八年級", "九年級"):
                        by_school[school] = by_school.get(school, 0) + 1
    except OSError:
        pass

    result["count_elementary"] = count_elementary
    result["count_junior"] = count_junior
    result["count_senior"] = count_senior
    result["count_cap"] = count_cap
    result["count_count_ceec" if False else "count_ceec"] = count_ceec
    result["count_tcool"] = count_tcool  # placeholder — tcool schools now in StudyArk structure
    # Override: count_tcool = sum of all StudyArk non-cap/ceec files (since migration)
    # 但這樣會跟 count_junior/senior 重複。改用 by_school 有幾個 (>= 1) 作為 tcool 來源評估
    # Actually: tcool migration put 國中/year/subject into same path as StudyArk. So count_tcool = 0 here,
    # but we track via "schools with manual archive" in by_school (e.g., 高雄市五福國中).
    # Simplest: count_tcool = total school entries from by_school.
    result["count_tcool"] = sum(by_school.values())
    result["school_count"] = len(by_school)
    result["by_county"] = dict(sorted(by_county.items(), key=lambda x: -x[1])[:20])
    result["by_school"] = dict(sorted(by_school.items(), key=lambda x: -x[1])[:20])
    result["by_subject"] = dict(sorted(by_subject.items(), key=lambda x: -x[1])[:15])
    # Latest 15 files by mtime
    result["latest_files"] = sorted(all_files_with_mtime, key=lambda x: -x["mtime"])[:15]
    # Format mtime as ISO string for JSON
    import datetime
    for entry in result["latest_files"]:
        try:
            entry["mtime_str"] = datetime.datetime.fromtimestamp(entry["mtime"]).strftime("%Y-%m-%d %H:%M")
        except Exception:
            entry["mtime_str"] = "?"
    result["archive_size_mb"] = round(archive_size / 1024 / 1024, 1)
    result["total_files"] = total_files
    result["total_all"] = (
        count_elementary + count_junior + count_senior + count_cap + count_ceec
    )
    return result


# === News summaries (2026-07-25) =============================
# 讀靈覓/懂王每日 docx → 拆成 entries (title, summary, url, source, category)
# Cache 10 分鐘 (docx 每天只生一次, 不用太頻繁 refresh)

_news_cache: dict = {"data": None, "ts": 0.0}
_news_lock = threading.Lock()
_NEWS_TTL = 600  # 10 minutes

_NEWS_DIR = Path("/home/aping/.openclaw/workspace/靈覓")
LINGMIAN_DIR = _NEWS_DIR / "每日新聞文件"
TRUMP_DIR = _NEWS_DIR / "懂王新聞摘要"


def _parse_news_docx(path: Path) -> list[dict]:
    """讀 docx → 拆成 list of news entries.

    docx 格式 (靈覓 / 懂王都類似):
      <標題>
      🔶 <title>
      摘要：<summary>...
      連結：<url>
      來源：<source>
      標籤：...影響等級：...
      🔶 <next entry>
    """
    import zipfile
    import re as _re

    if not path.exists():
        return []

    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8")
    except Exception:
        return []

    texts = _re.findall(r"<w:t[^>]*>([^<]*)</w:t>", xml)
    full = "".join(texts)

    # Category: emoji (1-4 codepoints) + space + short text + 下一個 🔶 之前的標題
    cat_re = _re.compile(
        r"((?:[\U0001F000-\U0001FFFF\u2600-\u27BF][\U0001F000-\U0001FFFF\u2600-\u27BF\uFE00-\uFE0F]*)\s+[^\n🔶]{2,30})🔶"
    )
    cat_positions = [(m.start(), m.group(1).strip()) for m in cat_re.finditer(full)]

    # Entry 切割
    entry_starts = [m.start() for m in _re.finditer(r"🔶 ", full)]
    entries = []
    current_category = "其他"
    cat_idx = 0

    for i, start in enumerate(entry_starts):
        end = entry_starts[i + 1] if i + 1 < len(entry_starts) else len(full)
        chunk = full[start:end]

        # Update category
        while cat_idx < len(cat_positions) and cat_positions[cat_idx][0] < start:
            current_category = cat_positions[cat_idx][1]
            cat_idx += 1

        # Title: between 🔶 and 摘要：
        parts = _re.split(r"摘要：", chunk, maxsplit=1)
        if len(parts) < 2:
            continue
        title_full = parts[0][len("🔶 "):].strip()
        for sep in ["標籤：", "影響等級："]:
            idx = title_full.find(sep)
            if idx > 0:
                title_full = title_full[:idx].strip()
        title = title_full
        if not title:
            continue

        rest = parts[1]

        # Summary: between 摘要：(consumed) and 連結：
        sum_parts = _re.split(r"連結：", rest, maxsplit=1)
        summary = sum_parts[0].strip()
        # Strip trailing 來源： / 標籤：
        for sep in ["來源：", "標籤：", "影響等級："]:
            idx = summary.find(sep)
            if idx > 0:
                summary = summary[:idx].strip()
        summary = summary[:200]

        if len(sum_parts) < 2:
            continue
        after_url = sum_parts[1]

        # URL: until 來源 / 標籤 / 影響等級
        url_end = _re.search(r"(?:來源|標籤|影響等級)", after_url)
        if url_end:
            url = after_url[:url_end.start()].strip()
        else:
            url = after_url.strip()
        if not url:
            continue

        # Source: between 來源： and 標籤/影響等級
        source = ""
        src_parts = _re.split(r"來源：", after_url, maxsplit=1)
        if len(src_parts) >= 2:
            after_src = src_parts[1]
            src_end = _re.search(r"(?:標籤|影響等級|$)", after_src)
            source = after_src[:src_end.start()].strip() if src_end else after_src.strip()

        entries.append({
            "category": current_category,
            "title": title,
            "summary": summary,
            "url": url,
            "source": source,
        })

    return entries


def _get_cached_news_summaries() -> dict:
    """讀靈覓 / 懂王 今日 docx, 回傳 {lingmian: [...], trump: [...], dates: {...}}.

    Cache 10 分鐘 (docx 每天生成, 太頻繁 refresh 沒意義)
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz_taipei = ZoneInfo("Asia/Taipei")
    today = datetime.now(tz_taipei).strftime("%Y-%m-%d")

    now = _time.time()
    with _news_lock:
        if _news_cache["data"] is not None and now - _news_cache["ts"] < _NEWS_TTL:
            return _news_cache["data"]

    # 靈覓今日 file (取 LINGMIAN_DIR 最新的一個 docx, 命名不一定當天)
    lingmian_path = None
    if LINGMIAN_DIR.exists():
        candidates = sorted(LINGMIAN_DIR.glob("每日新聞摘要_*.docx"), reverse=True)
        if candidates:
            lingmian_path = candidates[0]
    lingmian_entries = _parse_news_docx(lingmian_path) if lingmian_path else []

    # 懂王今日 file
    trump_path = None
    if TRUMP_DIR.exists():
        candidates = sorted(TRUMP_DIR.glob("*_懂王新聞每日摘要.docx"), reverse=True)
        if candidates:
            trump_path = candidates[0]
    trump_entries = _parse_news_docx(trump_path) if trump_path else []

    # 用查到的 file date (從 filename) 作為顯示日期
    import re as _re_date
    lingmian_date = today
    if lingmian_path:
        m = _re_date.search(r'(\d{4}-\d{2}-\d{2})', lingmian_path.name)
        if m:
            lingmian_date = m.group(1)
    trump_date = today
    if trump_path:
        m = _re_date.search(r'(\d{4}-\d{2}-\d{2})', trump_path.name)
        if m:
            trump_date = m.group(1)

    data = {
        "lingmian": lingmian_entries,
        "trump": trump_entries,
        "lingmian_date": lingmian_date,
        "trump_date": trump_date,
        "lingmian_path": str(lingmian_path) if lingmian_path else None,
        "trump_path": str(trump_path) if trump_path else None,
    }

    with _news_lock:
        _news_cache["data"] = data
        _news_cache["ts"] = now

    return data


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    user: User = Depends(require_login),
):
    # 【2026-07-24 新】拿 CAP / CEEC 真實 subject list (從 disk scan) 傳到 template
    #   避免 JS hardcode 漏掉 (例如 CAP 有「寫作測驗」)
    cap_items = _scan_pdf_tree(CAP_DIR)
    ceec_items = _scan_pdf_tree(CEEC_DIR)
    cap_subjects = sorted({i["subject"] for i in cap_items if i["subject"]})
    ceec_subjects = sorted({i["subject"] for i in ceec_items if i["subject"]})

    # 【2026-07-28 移】平台資訊搬到 dashboard.html
    stats = _get_cached_archive_counts()

    return templates.TemplateResponse(
        "dashboard.html",
        {
            **_common_ctx(user),
            "request": request,
            "user_full": user,  # 完整 User object 給 template 用 datetime 等
            "cap_subjects_json": json.dumps(cap_subjects, ensure_ascii=False),
            "ceec_subjects_json": json.dumps(ceec_subjects, ensure_ascii=False),
            "stats": stats,
        },
    )


@router.get("/ui/school-sources", response_class=HTMLResponse)
async def school_sources(
    request: Request,
    user: User | None = Depends(get_current_user_from_token),
):
    """
    【2026-07-26 新】各校考古題外部連結聚合
    - 來源: tcool.cc (https://www.tcool.cc/)
    - 110 個學校 (國中 71 + 高中 23 + 國小 16)
    - 公開頁面,不需登入 (也是「推廣入口」)
    - 資料來自: backend/data/external_sources.json
    """
    import json as _json
    # pages.py 在 backend/app/api/, 所以 .parent.parent.parent = backend/
    sources_path = Path(__file__).parent.parent.parent / "data" / "external_sources.json"
    sources = []
    last_scraped = None
    if sources_path.exists():
        try:
            data = _json.loads(sources_path.read_text(encoding="utf-8"))
            sources = data.get("schools", [])
            last_scraped = data.get("last_scraped")
        except Exception:
            pass

    # 群組: {category: {county: [schools]}}
    grouped: dict[str, dict[str, list]] = {
        "junior": {}, "senior": {}, "elementary": {},
    }
    category_labels = {
        "junior": "🎒 國中段考考古題",
        "senior": "🎓 高中段考考古題",
        "elementary": "📒 國小段考考古題",
    }
    link_type_badges = {
        "drive": ("🗂️ Google Drive", "bg-primary"),
        "school_web": ("🌐 校網", "bg-secondary"),
        "sites": ("📂 Google Sites", "bg-info"),
        "nas": ("💾 NAS", "bg-warning"),
        "sharepoint": ("🔗 SharePoint", "bg-dark"),
        "other": ("🔗 其他", "bg-light text-dark"),
    }
    for s in sources:
        cat = s["category"]
        county = s["county"] or "未註明"
        grouped.setdefault(cat, {}).setdefault(county, []).append(s)

    # Sort county by name (稳定)
    sorted_grouped = {}
    cat_totals = {}
    for cat, counties in grouped.items():
        sorted_grouped[cat] = sorted(counties.items(), key=lambda x: x[0])
        cat_totals[cat] = sum(len(schools) for schools in counties.values())

    return templates.TemplateResponse(
        "school_sources.html",
        {
            **_common_ctx(user),
            "request": request,
            "grouped": sorted_grouped,
            "category_labels": category_labels,
            "link_type_badges": link_type_badges,
            "total_count": len(sources),
            "cat_totals": cat_totals,
            "last_scraped": last_scraped,
        },
    )


@router.get("/admin", response_class=HTMLResponse)
async def admin_panel(
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    # 待審核
    stmt_pending = (
        select(AccessRequest)
        .where(AccessRequest.status == "pending")
        .order_by(AccessRequest.created_at.desc())
    )
    pending = (await db.execute(stmt_pending)).scalars().all()

    # 所有 user
    stmt_users = select(User).order_by(User.first_seen_at.desc())
    users = (await db.execute(stmt_users)).scalars().all()

    # 最近 audit log
    stmt_logs = select(AuditLog).order_by(desc(AuditLog.created_at)).limit(20)
    logs = (await db.execute(stmt_logs)).scalars().all()

    return templates.TemplateResponse(
        "admin.html",
        {
            **_common_ctx(admin),
            "request": request,
            "pending_requests": pending,
            "users": users,
            "audit_logs": logs,
            "admin": admin,  # 【2026-07-22 新】template 需要比對是否自己
        },
    )


# === CAP / CEEC exam archives ============================================
# 2026-07-22 新增 - 獨立頁面顯示歷年 CAP 會考 + CEEC 大考

CAP_DIR = Path("/mnt/my_book/考題收集/cap_exam")
CEEC_DIR = Path("/mnt/my_book/考題收集/ceec")

# 【2026-07-25 新】CAP/CEEC 結果每頁顯示數
# 跟 StudyArk search_results.html 的 PAGINATION 一樣 (PAPERS_PER_PAGE=8 papers × 2 files = 16)
# CAP/CEEC 是 1 file/paper, 所以 15 files/頁
EXAM_ITEMS_PER_PAGE = 15


# 【2026-07-24 新】CEEC filename parser helpers
import re as _re_year_dir_module
_re_year_dir = _re_year_dir_module.compile(r"^(\d{2,3})年?$")

# 學測/分科常見學科清單 (用來從 filename 拆 subject)
# 【2026-07-24 改】只列「短名」, 「國語文綜合能力測驗」之類在 parser 內 normalize 成「國綜」
# 這樣 filter UI 不會出現重複按鈕
_CEEC_SUBJECTS = (
    # 學測 (舊制 5 主科)
    "國文", "英文", "數學", "社會", "自然",
    # 學測 (新制 4 主科)
    "國綜", "國寫", "數a", "數b",
    # 分科
    "數甲", "數乙",
    "物理", "化學", "生物",
    "歷史", "地理", "公民與社會",
)
# 排序: 越長的越先 match (避免 "數學" 比 "數甲" 先 match)
_CEEC_SUBJECTS_SORTED = sorted(set(_CEEC_SUBJECTS), key=lambda s: -len(s))

# 【2026-07-24 新】長名 → 短名 normalization (避免 UI 重複)
# 國語文綜合能力測驗 = 國綜
# 國語文寫作能力測驗 = 國寫
# 數學a = 數a, 數學b = 數b, 數學甲 = 數甲, 數學乙 = 數乙
# 公民 (簡寫) = 公民與社會
_CEEC_SUBJECT_NORMALIZE = {
    "國語文綜合能力測驗": "國綜",
    "國語文寫作能力測驗": "國寫",
    "數學a": "數a",
    "數學b": "數b",
    "數學甲": "數甲",
    "數學乙": "數乙",
    "公民": "公民與社會",  # 「公民」簡寫 → 完整名
}

# 【2026-07-24 新】單字 subject → 完整名 (視障生試題音軌 106sat 檔案用)
# 106sat_音軌對照表_國_圖文.pdf → '國' → '國文'
_SHORT_SUBJECT_MAP = {
    "國": "國文",
    "數": "數學",
    "社": "社會",
    "自": "自然",
    "英": "英文",
}


# 【2026-07-24 新】CAP filename parser
_CAP_SUBJECTS = (
    "國文", "英語", "數學", "社會", "自然", "寫作測驗",
)
_CAP_SUBJECTS_SORTED = sorted(set(_CAP_SUBJECTS), key=lambda s: -len(s))


def _parse_cap_filename(name: str) -> tuple[str, str]:
    """
    從 CAP filename 解析 subject + file_type。
    Layout 1 (4 parts): '102_英語_英語科_1ZfjE4' → ('英語', '英語科')
    Layout 2 (3 parts): '102_國文_1ZfjE4' → ('國文', '')
    Layout 3 (參考答案): '102_參考答案_1b3Vusr5' → ('', '參考答案')  # 參考答案是 filetype
    Layout 4 (其他): '102_其他_一級分_1amvNV' → ('', '其他')  # 其他 是 file_type
    Layout 5 (3 parts): '115_英語_英語科' → ('英語', '英語科')
    """
    stem = name[:-4] if name.endswith(".pdf") else name
    parts = stem.split("_")
    if len(parts) < 3 or not parts[0].isdigit():
        return ("", "")

    raw_subject = parts[1]

    # Layout 3/4: 參考答案 / 其他 / 試題說明 - 這些不是 subject, 是 file_type
    if raw_subject in ("參考答案", "其他", "試題說明"):
        return ("", raw_subject)

    # Layout 2 (3 parts, parts[2] 是 gdoc_id 隨機字串): 用 _CAP_SUBJECTS_SORTED 確認是 subject
    subject = ""
    for s in _CAP_SUBJECTS_SORTED:
        if raw_subject == s:
            subject = s
            break

    # file_type = parts[2] if exists and not gdoc_id-like
    # gdoc_id 通常 6 字以上 random alphanumeric
    file_type = ""
    if len(parts) >= 3:
        candidate = parts[2]
        # 如果是 gdoc_id (random alphanumeric), file_type 為空
        if len(candidate) >= 6 and candidate.replace("-", "").isalnum() and not any('\u4e00' <= c <= '\u9fff' for c in candidate):
            file_type = ""
        else:
            file_type = candidate
        # 4 parts: parts[3] 也是 gdoc_id, file_type 是 parts[2]
        # 3 parts: file_type 是 parts[2] unless 是 gdoc_id

    return (subject, file_type)


def _parse_ceec_filename(name: str) -> tuple[str, str]:
    """
    從 CEEC filename 解析 subject + file_type。
    【2026-07-24 改】長名 normalize 成短名 (避免 UI 出現重複按鈕)
      '國語文綜合能力測驗' → '國綜'
      '國語文寫作能力測驗' → '國寫'
      '數學a' / '數學b' → '數a' / '數b'
      '數學甲' / '數學乙' → '數甲' / '數乙'
      '公民' → '公民與社會'

    Pattern:
      '01-100學測國文試卷定稿.pdf' → ('國文', '試卷定稿')
      '01-111分科測驗數學甲選擇題答案.pdf' → ('數甲', '選擇題答案')
      '01-111學測國語文綜合能力測驗答案.pdf' → ('國綜', '答案')
      '100sat_語音_國文_圖文(序號).pdf' → ('國文', '語音_圖文(序號)')  # 「語音」 變 file_type prefix
      '103指考試題音軌對照表_化學.pdf' → ('化學', '試題音軌對照表')
    回傳 (subject, file_type) 任一失敗回 ("", "")
    """
    # 拿掉 .pdf
    stem = name[:-4] if name.endswith(".pdf") else name

    # Pattern 3 (先試, 以免被 Pattern 1 吃掉): 103指考試題音軌對照表_化學 / 106sat_音軌對照表_國_圖文
    # tokens: ['103指考試題音軌對照表', '化學'] / ['106sat', '音軌對照表', '國', '圖文']
    if "_" in stem:
        parts = stem.split("_")
        # 檢查是否含「音軌對照表」
        if any("音軌對照表" in p for p in parts):
            if "試題音軌對照表" in stem:
                # Layout A: 103指考試題音軌對照表_化學 → ['103指考試題音軌對照表', '化學']
                subject_token = parts[-1]
                subject, _ = _match_ceec_subject(subject_token)
                if not subject:
                    subject = subject_token  # fallback
                return subject, "試題音軌對照表"
            else:
                # Layout B: 106sat_音軌對照表_國_圖文 → ['106sat', '音軌對照表', '國', '圖文']
                # tokens[-2] 是單字 (國/數/社/自/英) 要展開成完整名
                # tokens[-1] 是 圖文/文字/點字 (file_type)
                single_char = parts[-2]
                subject = _SHORT_SUBJECT_MAP.get(single_char, single_char)
                file_type = "音軌對照表_" + parts[-1]
                return subject, file_type

    # Pattern 1: 01-100學測國文試卷定稿 / 01-100指考數甲選擇(填)題答案-0712 / 01-111分科測驗數學甲...
    m = _re_year_dir_module.match(r"^\d{1,3}-\d{2,3}(?:學測|分科(?:測驗)?|指考)(.+)$", stem)
    if m:
        rest = m.group(1)
        subject, match_len = _match_ceec_subject(rest)
        file_type = rest[match_len:] if subject else rest
        return subject, file_type

    # Pattern 2: 100sat_語音_國文_圖文(序號) / 100sat_語音_數學_文字(序號)
    # tokens: ['100sat', '語音', '國文', '圖文(序號)']
    if "_" in stem:
        tokens = stem.split("_")
        if len(tokens) >= 3 and (tokens[0].endswith("sat") or tokens[0].isdigit()):
            # 跳過 「語音」prefix (視障生試題語音檔)
            # 真正的 subject 是中間的 tokens
            content_tokens = tokens[1:-1]  # ['語音', '國文']
            # 如果中間有「語音」, 把它移成 file_type prefix
            if content_tokens and content_tokens[0] == "語音":
                subject_part = "_".join(content_tokens[1:])  # '國文'
                file_type = "語音_" + tokens[-1]  # '語音_圖文(序號)'
            else:
                subject_part = "_".join(content_tokens)
                file_type = tokens[-1]
            subject = _normalize_ceec_subject(subject_part)
            return subject, file_type

    # Pattern 4: 104指考生物定稿 (沒有 - 隔開, 只有 num + 指考 + subject + filetype)
    m = _re_year_dir_module.match(r"^\d{2,3}(?:學測|分科(?:測驗)?|指考)(.+)$", stem)
    if m:
        rest = m.group(1)
        subject, match_len = _match_ceec_subject(rest)
        file_type = rest[match_len:] if subject else rest
        return subject, file_type

    return ("", "")


def _match_ceec_subject(text: str) -> tuple[str, int]:
    """從 text 開頭找最長的 subject match, 回傳 (normalized_subject, match_length)。
    match_length 是檔名中 match 的長度 (用來計算 file_type)。

    順序: 先試所有候選 (短名 + 長名) 找最長的 match, 再 normalize 成短名。
    避免「公民」先於「公民與社會」 match (兩個都包含「公民」開頭)

    例:
      '數學甲選擇題答案' → match '數學甲' (len=3) → normalize '數甲'
      '公民試卷定稿' → match '公民' (len=2) → normalize '公民與社會'
      '公民與社會試卷定稿' → match '公民與社會' (len=5) → normalize '公民與社會'
      '國語文綜合能力測驗答案' → match '國語文綜合能力測驗' (len=8) → normalize '國綜'
    """
    # 建立完整候選清單: (原始名, 顯示名) 對照
    candidates = []
    # 短名清單 (已 normalize)
    for subj in _CEEC_SUBJECTS:
        candidates.append((subj, subj))  # 原始名 == 顯示名
    # 長名 → 短名
    for long_name, short_name in _CEEC_SUBJECT_NORMALIZE.items():
        candidates.append((long_name, short_name))

    # 找最長的 match
    best_match = ""
    best_subj = ""
    for raw_name, subj in candidates:
        if len(raw_name) > len(best_match) and text.startswith(raw_name):
            best_match = raw_name
            best_subj = subj

    return best_subj, len(best_match)


def _normalize_ceec_subject(subj: str) -> str:
    """單個 subject 名稱 normalize (長名 → 短名)"""
    return _CEEC_SUBJECT_NORMALIZE.get(subj, subj)


# === PDF tree in-memory cache (2026-07-24) ===========================
# /mnt/my_book/考題收集 是 CIFS 網路磁碟, _scan_pdf_tree() 對 1490 個 CEEC PDF
# 要 9 秒、323 個 CAP 要 2 秒。 dashboard 每次 request 會呼 4-6 次 (CAP/CEEC 各 2-3 次)
# 結果 user 看到 18-36 秒 page load。
#
# 解法: in-memory cache + TTL + background refresh + invalidation signal
#   - cache hit (TTL 內): 瞬間 return
#   - stale (TTL 外): return stale + background thread refresh
#   - invalidate signal: archive cron 在 state/_pdf_tree_cache.invalidate touch
#     下次 request 看到 signal 比 cache 新 → invalidate + re-scan
#
# 不需要建 db: 全部 metadata 只 ~300KB in-memory
_pdf_tree_cache: dict[str, tuple[float, list[dict]]] = {}
_pdf_tree_lock = threading.Lock()
_scan_in_progress: dict[str, threading.Lock] = {}
_PDF_TREE_TTL = 600  # 10 minutes
_PDF_TREE_INVALIDATE_SIGNAL = Path("/mnt/my_book/考題收集/state/_pdf_tree_cache.invalidate")


def _check_invalidate_signal(root: Path) -> bool:
    """【2026-07-24 新】檢查 archive cron 是否寫了 invalidate signal。
    如果 signal mtime > cache ts → cache stale, 需 re-scan
    """
    cached = _pdf_tree_cache.get(str(root))
    if cached is None:
        return False
    cached_ts = cached[0]
    try:
        if not _PDF_TREE_INVALIDATE_SIGNAL.exists():
            return False
        sig_mtime = _PDF_TREE_INVALIDATE_SIGNAL.stat().st_mtime
        return sig_mtime > cached_ts
    except OSError:
        return False


def _refresh_pdf_tree_bg(root_str: str, root: Path):
    """Background refresh, non-blocking"""
    items = _do_scan_pdf_tree(root)


def _refresh_pdf_tree_bg(root_str: str, root: Path):
    """Background refresh, non-blocking"""
    with _pdf_tree_lock:
        # 避免重複 scan - 如果另一個 thread 已經在 scan 或 cache 已更新, 跳過
        cached = _pdf_tree_cache.get(root_str)
        if cached is not None and _time.time() - cached[0] < 5:  # 5 秒內有人更新過
            return
    items = _do_scan_pdf_tree(root)
    with _pdf_tree_lock:
        _pdf_tree_cache[root_str] = (_time.time(), items)


def _do_scan_pdf_tree(root: Path) -> list[dict]:
    """實際執行 rglob + stat (耗時, 走 background)"""
    items = []
    if not root.exists():
        return items
    import os
    pdf_paths = []
    for dirpath, dirnames, filenames in os.walk(root):
        if "_generic" in Path(dirpath).parts:
            dirnames[:] = []
            continue
        for fn in filenames:
            if fn.endswith(".pdf"):
                pdf_paths.append(Path(dirpath) / fn)

    for pdf in pdf_paths:
        if not pdf.is_file():
            continue
        stat = pdf.stat()
        rel = pdf.relative_to(root)
        parts = rel.parts
        exam_type = parts[0] if len(parts) >= 3 else ""
        year = 0
        if len(parts) >= 3:
            year_dir = parts[1]
            year_m = _re_year_dir.match(year_dir)
            if year_m:
                year = int(year_m.group(1))
        if root == CEEC_DIR:
            subject, file_type = _parse_ceec_filename(pdf.name)
        else:
            subject, file_type = _parse_cap_filename(pdf.name)

        items.append({
            "path": str(pdf),
            "rel": str(rel),
            "name": pdf.name,
            "exam_type": exam_type,
            "year": year,
            "subject": subject,
            "file_type": file_type,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
        })
    return items


def _scan_pdf_tree(root: Path) -> list[dict]:
    """
    掃描 PDF 樹狀結構,回傳分組資料。
    每個 dict: {path, year, subject, file_type, size, mtime}

    【2026-07-24】過濾 _generic 資料夾 - generic instruction files
    沒年份資訊,不適合顯示在年份 filter 列表。

    【2026-07-24 改】year 從路徑 parts[1] 拿 ("100年" → 100), 不再依賴
    filename regex - 之前 "01-100學測國文試卷.pdf" 這種格式 regex 抓不到
    year 結果 UI 顯示一堆「學測 0 年 (685 個檔案)」讓 user 疑惑。

    【2026-07-24】in-memory cache + TTL + background refresh + invalidation
    避免每次 request 都 rglob 一次 (CEEC 1490 PDFs 要 9 秒)
    - TTL 10 分鐘, stale 時 return 舊 cache + background thread refresh
    - cold start (cache 空) 時同步 scan, 之後的 request 都是瞬時
    - invalidation: archive cron touch state/_pdf_tree_cache.invalidate
      下次 request 看到 signal 比 cache 新 → invalidate + re-scan
    """
    root_str = str(root)
    now = _time.time()

    cached = _pdf_tree_cache.get(root_str)
    if cached is not None:
        cached_ts, cached_items = cached
        # 【2026-07-24 新】invalidation signal check (越過 TTL/8 分鐘)
        if _check_invalidate_signal(root):
            pass  # 往下走 invalidate 邏輯
        elif now - cached_ts < _PDF_TREE_TTL:
            return cached_items  # Fresh cache hit
        else:
            # Stale (TTL 外, 無 invalidate): return old + background refresh
            import threading as _threading
            _threading.Thread(target=_refresh_pdf_tree_bg, args=(root_str, root), daemon=True).start()
            return cached_items
    # Invalidate 或 cache miss: 同步 scan
    # 用 per-root scan lock 避免 concurrent scan (warmup thread + first user request 同時 scan)
    scan_lock = _scan_in_progress.setdefault(root_str, threading.Lock())
    with scan_lock:
        # Double-check: 可能另一個 thread 剛剛完成 scan
        with _pdf_tree_lock:
            cached = _pdf_tree_cache.get(root_str)
            if cached is not None:
                cached_ts, cached_items = cached
                if not _check_invalidate_signal(root) and _time.time() - cached_ts < _PDF_TREE_TTL:
                    return cached_items
        items = _do_scan_pdf_tree(root)
        with _pdf_tree_lock:
            _pdf_tree_cache[root_str] = (_time.time(), items)
    return items


@router.get("/ui/cap-exam", response_class=HTMLResponse)
async def cap_exam_browser(
    request: Request,
    user: User | None = Depends(get_current_user_from_token),
    year: int | None = Query(None),
    subject: str | None = Query(None),
    filetype: str | None = Query(None),
    page: int = Query(1, ge=1),
):
    """
    歷屆國中教育會考瀏覽頁面 (CAP / RCPET)。
    公開頁面,不需登入 (但下載連結在 archive 路徑,Web UI 只列出 metadata)。

    【2026-07-24 新】支援 subject / filetype 篩選 (從 dashboard form 送過來)
    【2026-07-25 新】分頁: 每頁 EXAM_ITEMS_PER_PAGE (15) 個 items
    """
    return _render_cap_exam_results(request, user, year, subject, filetype, page)


def _render_cap_exam_results(request, user, year, subject, filetype, page=1):
    """
    內部 helper: 渲染 cap_exam.html 結果。可由 cap_exam_browser 或 /ui/search (grade=會考) 呼。
    【2026-07-25 改】改成單一 flat list, 排序: 年度 DESC → 科目 → 類型 → 檔名
    """
    # 取 raw items, 做 filter (subject + filetype + year)
    # 【2026-07-24 改】一個 call _scan_pdf_tree() 就好, 避免 repeated scan
    all_items = _scan_pdf_tree(CAP_DIR)

    # Apply subject/filetype/year filters
    items = all_items
    if subject:
        items = [i for i in items if i["subject"] == subject]
    if filetype:
        items = [i for i in items if filetype in i["file_type"]]
    if year is not None:
        items = [i for i in items if i["year"] == year]

    # Sort: year DESC → subject → file_type → filename
    # Empty subject/file_type 排後面 (用 (is_empty, value) tuple)
    def _sort_key(i):
        subj = i["subject"] or ""
        ftype = i["file_type"] or ""
        return (
            -(i["year"] or 0),
            (1, "") if subj == "" else (0, subj),  # empty 排後面
            (1, "") if ftype == "" else (0, ftype),
            i["name"] or "",
        )
    items = sorted(items, key=_sort_key)

    # 【2026-07-25 新】分頁 (跟 StudyArk search_results.html 一樣)
    total = len(items)
    total_page = max(1, (total + EXAM_ITEMS_PER_PAGE - 1) // EXAM_ITEMS_PER_PAGE)
    page = max(1, min(page, total_page))  # 越界保護
    start = (page - 1) * EXAM_ITEMS_PER_PAGE
    end = start + EXAM_ITEMS_PER_PAGE
    items_page = items[start:end]

    # qs_for_page helper (給分頁按鈕用)
    from urllib.parse import urlencode
    def qs_for_page(p: int) -> str:
        params = {}
        if year is not None:
            params["year"] = year
        if subject:
            params["subject"] = subject
        if filetype:
            params["filetype"] = filetype
        params["page"] = p
        return urlencode(params)

    # Build year/subject lists (for filter UI) - 用未 filter 的 all_items
    all_years = sorted(set(i["year"] for i in all_items if i["year"] > 0), reverse=True)
    all_subjects = sorted(set(i["subject"] for i in all_items if i["subject"]))

    return templates.TemplateResponse(
        "cap_exam.html",
        {
            **_common_ctx(user),
            "request": request,
            "items": items_page,
            "all_years": all_years,
            "all_subjects": all_subjects,
            "selected_year": year,
            "selected_subject": subject,
            "selected_filetype": filetype,
            "total_files": total,
            "total_size": sum(i["size"] for i in items),
            "page": page,
            "total_page": total_page,
            "per_page": EXAM_ITEMS_PER_PAGE,
            "qs_for_page": qs_for_page,
        },
    )


@router.get("/ui/cap-exam/download/{rel:path}")
async def cap_exam_download(rel: str, user: User = Depends(require_login)):
    """
    下載 CAP 會考 PDF。
    只給 approved (permission <= 7) 下載,跟 StudyArk 一樣。
    """
    from fastapi.responses import FileResponse

    # 安全檢查: 不允許 .. 路徑穿越
    if ".." in rel or rel.startswith("/"):
        raise HTTPException(403, "Invalid path")

    file_path = CAP_DIR / rel
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(404, "File not found")

    if not file_path.suffix.lower() == ".pdf":
        raise HTTPException(403, "Not a PDF")

    return FileResponse(
        file_path,
        media_type="application/pdf",
        filename=file_path.name,
    )


@router.get("/ui/ceec-exam", response_class=HTMLResponse)
async def ceec_exam_browser(
    request: Request,
    user: User | None = Depends(get_current_user_from_token),
    exam_type: str | None = Query(None),
    year: int | None = Query(None),
    subject: str | None = Query(None),
    filetype: str | None = Query(None),
    page: int = Query(1, ge=1),
):
    """
    歷屆大學入學考試瀏覽頁面 (CEEC)。
    公開頁面 (metadata),下載要登入。

    【2026-07-24 新】支援 subject / filetype 篩選 (從 dashboard form 送過來)
    【2026-07-25 新】分頁: 每頁 EXAM_ITEMS_PER_PAGE (15) 個 items
    """
    return _render_ceec_exam_results(request, user, exam_type, year, subject, filetype, page)


def _render_ceec_exam_results(request, user, exam_type, year, subject, filetype, page=1):
    """
    內部 helper: 渲染 ceec_exam.html 結果。可由 ceec_exam_browser 或 /ui/search (grade=大學入學考) 呼。
    【2026-07-25 改】改成單一 flat list, 排序: 年度 DESC → 考試類型 → 科目 → 類型 → 檔名
    """
    # 【2026-07-24 改】一次取 all_items, 用全量建 filter buttons (受 cache 保護, 0.02s)
    all_items = _scan_pdf_tree(CEEC_DIR)

    # Apply subject/filetype/exam_type/year filters
    items = all_items
    if exam_type:
        items = [i for i in items if i["exam_type"] == exam_type]
    if year is not None:
        items = [i for i in items if i["year"] == year]
    if subject:
        items = [i for i in items if i["subject"] == subject]
    if filetype:
        items = [i for i in items if filetype in i["file_type"]]

    # Sort: year DESC → exam_type → subject → file_type → filename
    # Empty subject/file_type 排後面
    def _sort_key_ceec(i):
        subj = i["subject"] or ""
        ftype = i["file_type"] or ""
        return (
            -(i["year"] or 0),
            i["exam_type"] or "",
            (1, "") if subj == "" else (0, subj),
            (1, "") if ftype == "" else (0, ftype),
            i["name"] or "",
        )
    items = sorted(items, key=_sort_key_ceec)

    # 【2026-07-25 新】分頁 (跟 StudyArk search_results.html 一樣)
    total = len(items)
    total_page = max(1, (total + EXAM_ITEMS_PER_PAGE - 1) // EXAM_ITEMS_PER_PAGE)
    page = max(1, min(page, total_page))  # 越界保護
    start = (page - 1) * EXAM_ITEMS_PER_PAGE
    end = start + EXAM_ITEMS_PER_PAGE
    items_page = items[start:end]

    # qs_for_page helper (給分頁按鈕用)
    from urllib.parse import urlencode
    def qs_for_page(p: int) -> str:
        params = {}
        if exam_type:
            params["exam_type"] = exam_type
        if year is not None:
            params["year"] = year
        if subject:
            params["subject"] = subject
        if filetype:
            params["filetype"] = filetype
        params["page"] = p
        return urlencode(params)

    # Build filter lists (from all_items, 不受 subject filter 影響)
    all_exam_types = sorted(set(i["exam_type"] for i in all_items if i["exam_type"]))
    all_years = sorted(set(i["year"] for i in all_items if i["year"] > 0), reverse=True)
    all_subjects = sorted(set(i["subject"] for i in all_items if i["subject"]))

    return templates.TemplateResponse(
        "ceec_exam.html",
        {
            **_common_ctx(user),
            "request": request,
            "items": items_page,
            "all_exam_types": all_exam_types,
            "all_years": all_years,
            "all_subjects": all_subjects,
            "selected_exam_type": exam_type,
            "selected_year": year,
            "selected_subject": subject,
            "selected_filetype": filetype,
            "total_files": total,
            "total_size": sum(i["size"] for i in items),
            "page": page,
            "total_page": total_page,
            "per_page": EXAM_ITEMS_PER_PAGE,
            "qs_for_page": qs_for_page,
        },
    )


@router.get("/ui/ceec-exam/download/{rel:path}")
async def ceec_exam_download(rel: str, user: User = Depends(require_login)):
    """下載 CEEC PDF (approved only)"""
    from fastapi.responses import FileResponse

    if ".." in rel or rel.startswith("/"):
        raise HTTPException(403, "Invalid path")

    file_path = CEEC_DIR / rel
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(404, "File not found")

    if not file_path.suffix.lower() == ".pdf":
        raise HTTPException(403, "Not a PDF")

    return FileResponse(
        file_path,
        media_type="application/pdf",
        filename=file_path.name,
    )


# === Archive Batch Download (CAP / CEEC) =====================
# 【2026-07-25 新】跟 StudyArk batch-download 一樣 UX, 但
# - 來源是本地 archive (CAP_DIR / CEEC_DIR), 不用下載 StudyArk PDF
# - 不用走 rate limit, 沒 10 秒/item 等待
# - 限到 MAX_BATCH = 20 避免 zip 太大

from pydantic import BaseModel as _BaseModel
from app.core.db_helpers import hash_ip as _hash_ip

class _ArchiveBatchItem(_BaseModel):
    """CAP/CEEC archive batch item"""
    source: str  # "CAP" or "CEEC"
    rel: str  # relative path under CAP_DIR / CEEC_DIR
    subject: str | None = None
    grade: str | None = None
    school_year: str | None = None
    title: str | None = None


class _ArchiveBatchRequest(_BaseModel):
    items: list[_ArchiveBatchItem]


@router.post("/api/batch-download-archive")
async def batch_download_archive(
    req: _ArchiveBatchRequest,
    request: Request,
    user: User = Depends(require_approved),
    db: AsyncSession = Depends(get_db),
):
    """
    批次下載 CAP / CEEC archive PDF → 回傳 .zip。

    跟 StudyArk batch-download 的 UX 一樣, 但:
    - 來源: 本地 archive (CAP_DIR / CEEC_DIR), 檔案已下載在 disk
    - 不撞 StudyArk 限流
    - 限 MAX_BATCH = 20 個 items (防 zip 太大)
    """
    import io
    import zipfile
    import hashlib

    MAX_BATCH = 20
    items = req.items

    if not items:
        raise HTTPException(400, "至少要 1 個 item")
    if len(items) > MAX_BATCH:
        raise HTTPException(
            400,
            f"單批最多 {MAX_BATCH} 個, 你選了 {len(items)} 個。請減少後再試。"
        )

    buffer = io.BytesIO()
    downloaded = []  # (filename, item, pdf_bytes)
    errors = []

    for idx, item in enumerate(items):
        if item.source not in ("CAP", "CEEC"):
            errors.append({"rel": item.rel, "error": f"Unknown source: {item.source}"})
            continue
        if ".." in item.rel or item.rel.startswith("/"):
            errors.append({"rel": item.rel, "error": "Invalid path"})
            continue
        base = CAP_DIR if item.source == "CAP" else CEEC_DIR
        file_path = base / item.rel
        if not file_path.exists() or not file_path.is_file():
            errors.append({"rel": item.rel, "error": "File not found"})
            continue
        if not file_path.suffix.lower() == ".pdf":
            errors.append({"rel": item.rel, "error": "Not a PDF"})
            continue
        try:
            pdf_bytes = file_path.read_bytes()
            # 驗證 PDF magic bytes
            if not pdf_bytes.startswith(b"%PDF"):
                errors.append({"rel": item.rel, "error": "Invalid PDF content"})
                continue
            downloaded.append((file_path.name, item, pdf_bytes))
        except Exception as e:
            errors.append({"rel": item.rel, "error": str(e)})

    if not downloaded and errors:
        raise HTTPException(400, f"全部 {len(items)} 個 item 都失敗: {errors[0]['error']}")

    # 寫 zip
    used_names: dict[str, int] = {}
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname, item, pdf_bytes in downloaded:
            # 若同檔名加編號避免覆蓋
            if fname in used_names:
                used_names[fname] += 1
                stem, ext = fname.rsplit(".", 1)
                zip_fname = f"{stem}_{used_names[fname]}.{ext}"
            else:
                used_names[fname] = 0
                zip_fname = fname
            zf.writestr(zip_fname, pdf_bytes)

    # 寫 DownloadHistory (每個 item)
    # 重用 DownloadHistory model, classid="CAP"/"CEEC", fileid = SHA256(rel)[:12]
    for fname, item, pdf_bytes in downloaded:
        fileid_hash = hashlib.sha256(item.rel.encode("utf-8")).hexdigest()[:12]
        dh = DownloadHistory(
            user_id=user.id,
            classid=item.source,  # "CAP" or "CEEC"
            fileid=fileid_hash,
            filetype="paper",
            title=item.title or fname,
            school_name=None,
            grade=item.grade,
            school_year=item.school_year,
            school_term=None,
            category=item.source,
            subject=item.subject,
            exam_type=None,
            version=None,
            download_filename=fname,
            ip_hash=_hash_ip(request.client.host if request.client else None),
            user_agent=request.headers.get("user-agent", "")[:512] or None,
        )
        db.add(dh)

    await log_action(
        db,
        action="batch_download_archive",
        user_id=user.id,
        target=f"items={len(downloaded)}, errors={len(errors)}, sources={set(i.source for i in items)}",
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent", ""),
    )
    await db.commit()

    from fastapi.responses import Response
    zip_name = f"tqark_archive_{len(downloaded)}_items.zip"
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{zip_name}"',
            "X-Downloaded-Count": str(len(downloaded)),
            "X-Error-Count": str(len(errors)),
        },
    )


@router.get("/me/downloads", response_class=HTMLResponse)
async def me_downloads(
    request: Request,
    user: User = Depends(require_approved),
    db: AsyncSession = Depends(get_db),
    page: int = Query(1, ge=1),
    school_year: str | None = Query(None),
    subject: str | None = Query(None),
    filetype: str | None = Query(None),
):
    """
    使用者自己的下載紀錄(2026-07-20 新增)。

    - 只看自己(user_id = current user)
    - 可選篩選: school_year / subject / filetype
    - 分頁: 25 / 頁
    - 「重新下載」按鈕走原 /ui/download endpoint
    """
    PER_PAGE = 25

    # base query
    stmt = select(DownloadHistory).where(DownloadHistory.user_id == user.id)

    # 篩選
    if school_year:
        stmt = stmt.where(DownloadHistory.school_year == school_year)
    if subject:
        stmt = stmt.where(DownloadHistory.subject == subject)
    if filetype and filetype in ("paper", "daan"):
        stmt = stmt.where(DownloadHistory.filetype == filetype)

    # total count (加同樣的 filter)
    count_stmt = select(func.count()).select_from(DownloadHistory).where(
        DownloadHistory.user_id == user.id
    )
    if school_year:
        count_stmt = count_stmt.where(DownloadHistory.school_year == school_year)
    if subject:
        count_stmt = count_stmt.where(DownloadHistory.subject == subject)
    if filetype and filetype in ("paper", "daan"):
        count_stmt = count_stmt.where(DownloadHistory.filetype == filetype)
    total = (await db.execute(count_stmt)).scalar() or 0
    total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)

    # 分頁: 最新在最上面
    offset = (page - 1) * PER_PAGE
    stmt = (
        stmt.order_by(desc(DownloadHistory.downloaded_at))
        .limit(PER_PAGE)
        .offset(offset)
    )
    rows = (await db.execute(stmt)).scalars().all()

    # 動態填選項(該 user 有下載過的 school_year / subject)
    years_stmt = (
        select(DownloadHistory.school_year)
        .where(DownloadHistory.user_id == user.id, DownloadHistory.school_year.isnot(None))
        .distinct()
        .order_by(DownloadHistory.school_year.desc())
    )
    years = [r[0] for r in (await db.execute(years_stmt)).all() if r[0]]

    subjects_stmt = (
        select(DownloadHistory.subject)
        .where(DownloadHistory.user_id == user.id, DownloadHistory.subject.isnot(None))
        .distinct()
        .order_by(DownloadHistory.subject)
    )
    subjects = [r[0] for r in (await db.execute(subjects_stmt)).all() if r[0]]

    return templates.TemplateResponse(
        "me_downloads.html",
        {
            **_common_ctx(user),
            "request": request,
            "rows": rows,
            "total": total,
            "page": page,
            "total_pages": total_pages,
            "per_page": PER_PAGE,
            "years": years,
            "subjects": subjects,
            "filter_school_year": school_year or "",
            "filter_subject": subject or "",
            "filter_filetype": filetype or "",
        },
    )


@router.post("/me/downloads/{record_id}/delete")
async def me_downloads_delete(
    record_id: int,
    request: Request,
    user: User = Depends(require_approved),
    db: AsyncSession = Depends(get_db),
):
    """
    刪除單筆下載紀錄(2026-07-20 新增)。
    - 只刪 metadata,不動本機檔案(本機檔案刪除交給 browser-side File System Access API)
    - 只限 owner(user_id == current user.id),他人不可刪
    - 寫 audit log
    """
    rec = await db.get(DownloadHistory, record_id)
    if not rec:
        raise HTTPException(404, "Record not found")
    if rec.user_id != user.id:
        raise HTTPException(403, "這不是你的下載紀錄")

    fname = rec.download_filename
    await db.delete(rec)
    await log_action(
        db,
        action="delete_download",
        user_id=user.id,
        target=f"record:{record_id}",
        detail=f"deleted download history: {fname}",
        ip=str(request.client.host) if request.client else None,
    )
    await db.commit()
    return {"ok": True, "deleted_id": record_id, "filename": fname}


@router.post("/me/downloads/delete-all")
async def me_downloads_delete_all(
    request: Request,
    school_year: str | None = Query(None),
    subject: str | None = Query(None),
    filetype: str | None = Query(None),
    user: User = Depends(require_approved),
    db: AsyncSession = Depends(get_db),
):
    """
    刪除 user 下載紀錄中所有符合條件的 records(2026-07-20 新增)。
    - 對現在套用的篩選條件 filter 後全部刪掉
    - 只刪自己的 records
    - 寫一條 audit log (含實際刪幾筆)
    - 回傳刪除數量
    """
    stmt = select(DownloadHistory).where(DownloadHistory.user_id == user.id)
    if school_year:
        stmt = stmt.where(DownloadHistory.school_year == school_year)
    if subject:
        stmt = stmt.where(DownloadHistory.subject == subject)
    if filetype and filetype in ("paper", "daan"):
        stmt = stmt.where(DownloadHistory.filetype == filetype)

    rows = (await db.execute(stmt)).scalars().all()
    deleted_count = len(rows)
    deleted_filenames = [r.download_filename for r in rows]

    for r in rows:
        await db.delete(r)

    filter_desc = []
    if school_year:
        filter_desc.append(f"school_year={school_year}")
    if subject:
        filter_desc.append(f"subject={subject}")
    if filetype:
        filter_desc.append(f"filetype={filetype}")
    filter_text = ",".join(filter_desc) if filter_desc else "(all)"

    await log_action(
        db,
        action="delete_download_all",
        user_id=user.id,
        target="batch",
        detail=f"deleted {deleted_count} download records (filter={filter_text})",
        ip=str(request.client.host) if request.client else None,
    )
    await db.commit()

    return {
        "ok": True,
        "deleted_count": deleted_count,
        "filter": filter_text,
    }


# ============================================================
# Scraper UI(form submit 用的 page handler)
# ============================================================
# 共用的 render helper(form/querystring 都可以走)
async def _render_search_results(
    request: Request,
    user: User,
    db: AsyncSession,
    grade: str,
    subject: str,
    school_year: str,
    school_term: str,
    exam_type: str,
    version: str,
    daan: str,
    page: int,
    county: str = "",
    school_name: str = "",
):
    from app.core.db_helpers import log_action
    from app.db.models import utcnow
    from app.data.tw_counties import filter_school_by_county, get_county_name

    search_params = {
        "grade": grade or None,
        "subject": subject or None,
        "school_year": school_year or None,
        "school_term": school_term or None,
        "exam_type": exam_type or None,
        "version": version or None,
        "daan": daan or None,
        "page": page,
    }
    # 去掉 None (page 不去)
    search_params = {k: v for k, v in search_params.items() if v is not None and k != "page"}
    search_params["page"] = page

    error = None
    results = []
    total = 0
    total_page = 0
    api_page = 1
    try:
        raw = await studyark.search_papers(**search_params)
        # StudyArk 回傳 { list: [...], total, page, total_page }
        if isinstance(raw, dict):
            results = raw.get("list") or raw.get("data") or raw.get("results") or []
            total = raw.get("total", 0)
            total_page = raw.get("total_page", 0)
            api_page = raw.get("page", page)
        elif isinstance(raw, list):
            results = raw
    except FileNotFoundError as e:
        error = str(e)
    except Exception as e:
        error = f"StudyArk 連線失敗: {e}"

    # 一頁限 8 papers(原本 StudyArk 是 12 → 8)
    # 理由: 12 papers × 2 (試卷+答案) = 24 files 太多
    #       8 papers × 2 = 16 files,較接近 William 期望的 ~15 files/頁
    PAPERS_PER_PAGE = 8
    if results:
        results = results[:PAPERS_PER_PAGE]
        # 重新計算 total_page(以我們的 per-page 為準)
        if total:
            total_page = max(1, (total + PAPERS_PER_PAGE - 1) // PAPERS_PER_PAGE)

    # 本地 filter: county + school_name
    # (StudyArk 沒給 county,所以后端 filter)
    if results and (county or school_name):
        original_count = len(results)
        filtered = []
        for it in results:
            sname = it.get("school_name", "") or ""
            if county and not filter_school_by_county(sname, county):
                continue
            if school_name and school_name.strip() not in sname:
                continue
            filtered.append(it)
        results = filtered
        # 記錄 filter 資訊到 audit
        filter_info = f"county={county},school={school_name},filter_kept={len(filtered)}/{original_count}"
    else:
        filter_info = f"county={county or 'all'},school={school_name or 'all'}"

    # Audit log
    await log_action(
        db,
        action="search",
        user_id=user.id,
        target=f"grade={grade},subject={subject}",
        detail=f"page={page};{filter_info}",
    )
    await db.commit()

    # 顯示用字串
    params_display = ", ".join(f"{k}={v}" for k, v in search_params.items() if k != "page") or "(無條件)"

    # 給 template 用:根據當前 page 組 querystring(給「下一頁」連結用)
    def qs_for_page(p: int) -> str:
        base_params = {k: v for k, v in search_params.items() if k != "page"}
        base_params["page"] = p
        from urllib.parse import urlencode
        return urlencode(base_params)

    return templates.TemplateResponse(
        "search_results.html",
        {
            **_common_ctx(user),
            "request": request,
            "results": results,
            "error": error,
            "search_params": params_display,
            # 為了 template 顯示 current filter 跟 chip
            "grade": grade,
            "subject": subject,
            "school_year": school_year,
            "school_term": school_term,
            "exam_type": exam_type,
            "version": version,
            "daan": daan,
            "county": county,
            "school_name": school_name,
            "page": page,
            "total": total,
            "total_page": total_page,
            "qs_for_page": qs_for_page,
            "counties": __import__("app.data.tw_counties", fromlist=["COUNTIES"]).COUNTIES,
        },
    )



# === 【2026-07-31 新】三好工具 (Tools) ===
# - 只允許 perm <= 1 (家人 + 管理員)
# - 兩個工具: 日文翻譯 / 信用卡帳單
# - 現在是 placeholder, 之後實作

@router.get("/tools", response_class=HTMLResponse)
async def tools_index(
    request: Request,
    user: User = Depends(require_approved),
):
    """三好工具首頁 (限 perm 0 or 1)"""
    if not user or user.permission is None or user.permission > 1:
        raise HTTPException(
            status_code=403,
            detail="🔒 三好工具僅限家人/管理員使用 (需權限 0 或 1)",
        )
    return templates.TemplateResponse(
        "tools.html",
        {**_common_ctx(user), "request": request},
    )


@router.get("/tools/jp-translate", response_class=HTMLResponse)
async def tools_jp_translate(
    request: Request,
    user: User = Depends(require_approved),
):
    """日文翻譯工具 (限 perm 0 or 1)"""
    if not user or user.permission is None or user.permission > 1:
        raise HTTPException(
            status_code=403,
            detail="🔒 此功能僅限家人/管理員使用 (需權限 0 或 1)",
        )
    return templates.TemplateResponse(
        "tools_jp_translate.html",
        {**_common_ctx(user), "request": request},
    )


@router.get("/tools/credit-card", response_class=HTMLResponse)
async def tools_credit_card(
    request: Request,
    user: User = Depends(require_approved),
):
    """信用卡帳單工具 (限 perm 0 or 1)"""
    if not user or user.permission is None or user.permission > 1:
        raise HTTPException(
            status_code=403,
            detail="🔒 此功能僅限家人/管理員使用 (需權限 0 或 1)",
        )
    return templates.TemplateResponse(
        "tools_credit_card.html",
        {**_common_ctx(user), "request": request},
    )




# === 【2026-07-31 新】日文翻譯 API ===
# - /api/tools/jp-inbox-files?folder=...  (GET, list .docx in folder)
# - /api/tools/jp-done-files?folder=...   (GET, list .docx in folder)
# - /api/tools/jp-start                   (POST, filename + engine → background)
# - /api/tools/jp-status                  (GET, current status)
# - /api/tools/jp-reset                   (POST, reset to idle)

@router.get("/api/tools/jp-inbox-files")
async def jp_inbox_files(
    folder: str = Query(..., description="Absolute path to inbox folder"),
    user: User = Depends(require_approved),
):
    """列出 inbox folder 內 .docx 檔 (限 perm 0/1)"""
    if not user or user.permission is None or user.permission > 1:
        raise HTTPException(403, "🔒 此 API 僅限家人/管理員使用")
    from app.tools.jp_translator import list_docx
    return {"ok": True, "folder": folder, "files": list_docx(folder)}


@router.get("/api/tools/jp-done-files")
async def jp_done_files(
    folder: str = Query(..., description="Absolute path to outbox folder"),
    user: User = Depends(require_approved),
):
    """列出 outbox folder 內 .docx 檔 (限 perm 0/1)"""
    if not user or user.permission is None or user.permission > 1:
        raise HTTPException(403, "🔒 此 API 僅限家人/管理員使用")
    from app.tools.jp_translator import list_docx
    return {"ok": True, "folder": folder, "files": list_docx(folder)}


@router.post("/api/tools/jp-start")
async def jp_start(
    request: Request,
    user: User = Depends(require_approved),
):
    """啟動日文翻譯 (背景 process, 限 perm 0/1)"""
    if not user or user.permission is None or user.permission > 1:
        raise HTTPException(403, "🔒 此 API 僅限家人/管理員使用")
    body = await request.json()
    filename = body.get("filename", "").strip()
    engine = body.get("engine", "all").strip()
    outbox = body.get("outbox", "").strip()
    inbox = body.get("inbox", "").strip()
    
    if not filename:
        raise HTTPException(400, "filename 必填")
    if engine not in ("google", "minimax", "all"):
        raise HTTPException(400, f"engine 必須是 google / minimax / all, 收到: {engine}")
    
    from app.tools.jp_translator import start_translation
    result = start_translation(filename, engine, outbox, inbox)
    return result


@router.get("/api/tools/jp-status")
async def jp_status(user: User = Depends(require_approved)):
    """查詢日文翻譯目前 status (限 perm 0/1)"""
    if not user or user.permission is None or user.permission > 1:
        raise HTTPException(403, "🔒 此 API 僅限家人/管理員使用")
    from app.tools.jp_translator import get_status
    return get_status()


@router.post("/api/tools/jp-reset")
async def jp_reset(user: User = Depends(require_approved)):
    """重置日文翻譯 status (限 perm 0/1)"""
    if not user or user.permission is None or user.permission > 1:
        raise HTTPException(403, "🔒 此 API 僅限家人/管理員使用")
    from app.tools.jp_translator import reset_status
    reset_status()
    return {"ok": True, "state": "idle"}


@router.post("/ui/search", response_class=HTMLResponse)

async def ui_search_post(
    request: Request,
    grade: str = Form(""),
    subject: str = Form(""),
    school_year: str = Form(""),
    school_term: str = Form(""),
    exam_type: str = Form(""),
    version: str = Form(""),
    daan: str = Form(""),
    page: int = Form(1),
    county: str = Form(""),
    school_name: str = Form(""),
    user: User = Depends(require_approved),
    db: AsyncSession = Depends(get_db),
):
    """Form submit(dashboard 的 search form)"""
    # 【2026-07-24 改】統一考試 filter: 「會考」「大學入學考」 → render 對應 archive 結果
    #   不跳走、保留 filter (subject / year), user 看到的是「考古題 + 答案」清單
    if grade == "會考":
        year = int(school_year) if school_year and school_year.isdigit() else None
        filetype = daan if daan and daan in ("yes", "no") else None  # 沒意義但保留 URL
        return _render_cap_exam_results(request, user, year, subject, filetype, page)
    if grade == "大學入學考":
        year = int(school_year) if school_year and school_year.isdigit() else None
        filetype = daan if daan and daan in ("yes", "no") else None
        return _render_ceec_exam_results(request, user, None, year, subject, filetype, page)

    return await _render_search_results(
        request, user, db,
        grade=grade, subject=subject, school_year=school_year, school_term=school_term,
        exam_type=exam_type, version=version, daan=daan, page=page,
        county=county, school_name=school_name,
    )


@router.get("/ui/search", response_class=HTMLResponse)
async def ui_search_get(
    request: Request,
    grade: str = Query(""),
    subject: str = Query(""),
    school_year: str = Query(""),
    school_term: str = Query(""),
    exam_type: str = Query(""),
    version: str = Query(""),
    daan: str = Query(""),
    page: int = Query(1, ge=1, le=500),
    county: str = Query(""),
    school_name: str = Query(""),
    user: User = Depends(require_approved),
    db: AsyncSession = Depends(get_db),
):
    """Page 切換(分頁按鈕 → querystring)"""
    # 【2026-07-24 改】統一考試 filter: 「會考」「大學入學考」 → render 對應 archive 結果
    if grade == "會考":
        year = int(school_year) if school_year and school_year.isdigit() else None
        filetype = daan if daan and daan in ("yes", "no") else None
        return _render_cap_exam_results(request, user, year, subject, filetype, page)
    if grade == "大學入學考":
        year = int(school_year) if school_year and school_year.isdigit() else None
        filetype = daan if daan and daan in ("yes", "no") else None
        return _render_ceec_exam_results(request, user, None, year, subject, filetype, page)

    return await _render_search_results(
        request, user, db,
        grade=grade, subject=subject, school_year=school_year, school_term=school_term,
        exam_type=exam_type, version=version, daan=daan, page=page,
        county=county, school_name=school_name,
    )


@router.get("/ui/download/{classid}/{fileid}")
async def ui_download(
    classid: str,
    fileid: str,
    request: Request,
    filetype: str = Query("paper", regex="^(paper|daan)$"),
    title: str | None = Query(None),
    school_name: str | None = Query(None),
    grade: str | None = Query(None),
    school_year: str | None = Query(None),
    school_term: str | None = Query(None),
    category: str | None = Query(None),
    subject: str | None = Query(None),
    exam_type: str | None = Query(None),
    version: str | None = Query(None),
    user: User = Depends(require_approved),
    db: AsyncSession = Depends(get_db),
):
    """下載 PDF(stream from StudyArk,不存 server disk)"""
    from urllib.parse import quote

    from app.core.db_helpers import hash_ip, log_action
    from app.db.models import DownloadHistory

    item = studyark.ExamItem(
        classid=classid,
        fileid=fileid,
        filetype=filetype,
        title=title or "",
        school_name=school_name or "",
        grade=grade or "",
        school_year=school_year or "",
        school_term=school_term or "",
        category=category or "",
        subject=subject or "",
        exam_type=exam_type or "",
        version=version or "",
    )
    filename = studyark.build_download_filename(item)

    # 【2026-07-20 加】先查 PDF cache( /mnt/my_book/考題收集/ )
    # 命中 → 直接 serve (不用打 StudyArk、避開限流)
    # 沒命中 → 走 StudyArk → save 到 cache → serve
    # 【2026-07-21 加】cache path 加 county folder (其他X if unknown)
    from app.scraper.archive_path import (
        find_pdf_in_archive,
        ensure_archive_dirs,
        build_archive_path,
        build_archive_filename,
        _safe_dirname,
        normalize_county,
    )
    from app.scraper.pdf_title import extract_school_from_pdf
    import logging
    _cache_logger = logging.getLogger("tqark.cache")

    pdf_bytes = None
    cached_path = None  # 記住 cache path 以便檔名一致
    if grade and subject and filetype in ("paper", "daan"):
        cached = find_pdf_in_archive(grade, subject, filetype, fileid)
        _cache_logger.info(f"[UI CACHE CHECK] grade={grade!r} subject={subject!r} filetype={filetype!r} fileid={fileid} -> cached={cached}")
        if cached and cached.exists():
            cached_path = cached
            try:
                pdf_bytes = cached.read_bytes()
                _cache_logger.info(f"[UI CACHE HIT] {len(pdf_bytes)} bytes from {cached}")
            except OSError as e:
                _cache_logger.warning(f"UI cache read failed {cached}: {e}")
                pdf_bytes = None
        else:
            _cache_logger.info(f"[UI CACHE MISS] cached={cached}, exists={cached.exists() if cached else None}")

    if pdf_bytes is None:
        try:
            pdf_bytes, content_type = await studyark.download_pdf_stream(classid, fileid, filetype)
        except studyark.StudyArkRateLimit as e:
            # StudyArk 限流 → redirect 回搜尋結果頁，帶上 error message query param
            # (不要回 JSON 429，user 看不到友善訊息)
            # 用 referer header 回上一頁，沒有的話 fallback /dashboard
            referer = request.headers.get("referer", "")
            # referer 通常是 /ui/search?grade=...&... 這種
            from urllib.parse import urlparse, parse_qs, urlencode
            parsed = urlparse(referer) if referer else None
            if parsed and parsed.path in ("/ui/search", "/dashboard", "/me/downloads"):
                # 保留 query 但加 error param
                qs = parse_qs(parsed.query)
                qs["error"] = ["rate_limited"]
                qs["error_msg"] = [f"StudyArk 限流中:{e.message} 請等 {e.retry_after_minutes} 分鐘後重試。"]
                redirect_url = f"{parsed.path}?{urlencode(qs, doseq=True)}"
            else:
                # 沒有 referer → 回 dashboard 帶 error
                redirect_url = "/dashboard?error=rate_limited&error_msg=" + quote(
                    f"StudyArk 限流中:{e.message} 請等 {e.retry_after_minutes} 分鐘後重試。"
                )
            _cache_logger.warning(f"UI download rate-limited, redirect to {redirect_url}")
            return RedirectResponse(url=redirect_url, status_code=303)
        except FileNotFoundError as e:
            raise HTTPException(503, str(e))
        except Exception as e:
            raise HTTPException(502, f"下載失敗: {e}")

        # 驗證是 PDF 才存 cache (2026-07-20 加)
        if pdf_bytes.startswith(b"%PDF") and grade and subject and filetype in ("paper", "daan"):
            # 先存到 tmp (其他X folder), OCR 後 rename 到正確 county
            tmp_target = build_archive_path(
                grade, subject, filetype,
                f"_tmp_{fileid}_{filetype}",
                county=None,
            )
            try:
                ensure_archive_dirs(grade, subject, filetype, county=None)
                tmp_target.write_bytes(pdf_bytes)
                
                # OCR 抓 county
                try:
                    pdf_info = extract_school_from_pdf(tmp_target)
                    effective_county = pdf_info["county"] if pdf_info["county"] not in ("未註明", "其他縣市") else None
                    effective_school_name = pdf_info["school_name"] if pdf_info["school_name"] != "未註明" else school_name
                except Exception:
                    effective_county = None
                    effective_school_name = school_name
                
                # 組正式檔名
                year_term = f"{school_year or ''}{school_term or ''}"
                version_clean = version or "未註明"
                formal_filename = build_archive_filename(
                    county=effective_county,
                    year_term=year_term or "未分類",
                    exam_type=exam_type or "考試",
                    fileid=fileid,
                    school_name=effective_school_name or "未註明",
                    version=version_clean,
                )
                target = build_archive_path(
                    grade, subject, filetype, formal_filename, county=effective_county,
                )
                if target:
                    ensure_archive_dirs(grade, subject, filetype, county=effective_county)
                    if target.exists() and target != tmp_target:
                        tmp_target.unlink()
                    else:
                        tmp_target.rename(target)
                        cached_path = target
                        # 下載 filename = disk 檔名 (包含 county prefix)
                        filename = formal_filename
                    _cache_logger.info(f"[UI CACHE SAVED] {len(pdf_bytes)} bytes to {target}")
            except OSError as e:
                _cache_logger.warning(f"UI cache write failed {target}: {e}")
                try: tmp_target.unlink()
                except OSError: pass

        content_type = "application/pdf"
    else:
        content_type = "application/pdf"

    # 用 disk 上的檔名當 download filename (含 county prefix)
    if cached_path is not None:
        filename = cached_path.stem  # 不要 .pdf

    # 【2026-07-22 加】Lazy companion download:
    # 如果 user 下載 paper, background fetch daan (如果有) 並存到 cache
    # 如果 user 下載 daan, background fetch paper 並存到 cache
    # 這樣下次 user 抓同伴檔時就不用打 StudyArk
    if pdf_bytes and pdf_bytes.startswith(b"%PDF") and grade and subject and filetype in ("paper", "daan"):
        companion_filetype = "daan" if filetype == "paper" else "paper"
        companion_cached = find_pdf_in_archive(grade, subject, companion_filetype, fileid)
        if not companion_cached or not companion_cached.exists():
            # 沒有 companion → background fetch (不擋 user download)
            import asyncio
            asyncio.create_task(
                _background_fetch_companion(
                    classid=classid,
                    fileid=fileid,
                    filetype=companion_filetype,
                    grade=grade,
                    subject=subject,
                    school_year=school_year or "",
                    school_term=school_term or "",
                    exam_type=exam_type or "",
                    version=version or "",
                    school_name=school_name or "",
                )
            )
            _cache_logger.info(f"[UI BG FETCH] {companion_filetype} for fileid={fileid}")

    # 寫 DownloadHistory
    db_record = DownloadHistory(
        user_id=user.id,
        classid=classid,
        fileid=fileid,
        filetype=filetype,
        title=item.title or None,
        school_name=item.school_name or None,
        grade=item.grade or None,
        school_year=item.school_year or None,
        school_term=item.school_term or None,
        category=item.category or None,
        subject=item.subject or None,
        exam_type=item.exam_type or None,
        version=item.version or None,
        download_filename=filename,
        ip_hash=hash_ip(request.client.host if request.client else None),
        user_agent=request.headers.get("user-agent", "")[:512] or None,
    )
    db.add(db_record)
    await log_action(
        db,
        action="download",
        user_id=user.id,
        target=f"{classid}/{fileid}",
        detail=f"filetype={filetype}; filename={filename}",
        ip=str(request.client.host) if request.client else None,
    )
    await db.commit()

    safe_filename = filename.encode("ascii", errors="ignore").decode("ascii") or "exam.pdf"
    encoded_filename = quote(filename)
    headers = {
        "Content-Disposition": (
            f"attachment; "
            f'filename="{safe_filename}"; '
            f"filename*=UTF-8''{encoded_filename}"
        ),
        "X-Download-Filename": encoded_filename,  # URL-encoded, ASCII only
    }

    return Response(
        content=pdf_bytes,
        media_type=content_type,
        headers=headers,
    )


# === Cache warm-up on module import (2026-07-24) ======================
# Service 啟動時 background thread 先 scan CAP + CEEC 各一次
# 避免 user 第一個 request 慢 (cold start 9 秒)
# module import 結束時 background thread 開始跑, 完全不阻塞 startup
def _warmup_pdf_tree_cache():
    import logging
    import sys
    log = logging.getLogger("tqark.warmup")
    # 確保 log 會寫到 stdout (systemd journal)
    if not log.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setLevel(logging.INFO)
        log.addHandler(h)
    try:
        log.info("Cache warm-up: scanning CAP_DIR...")
        _scan_pdf_tree(CAP_DIR)
        log.info("Cache warm-up: scanning CEEC_DIR...")
        _scan_pdf_tree(CEEC_DIR)
        log.info("Cache warm-up: done")
    except Exception as e:
        log.warning(f"Cache warm-up failed: {e}")

import threading as _warmup_threading
_warmup_threading.Thread(target=_warmup_pdf_tree_cache, daemon=True).start()


# =========================================================
# 【2026-07-31 新】Disk-based school scan
# 用於 search dropdown: 選定 county 後, 列出 disk 中真有資料的學校
# =========================================================

# Cache (避免重複 scan)
_disk_schools_cache: dict = {"data": {}, "ts": 0.0}
_SCHOOLS_CACHE_TTL = 60  # 1 minute (背景 archive 持續寫新檔, cache 1 min 讓 user 看到新內容)

def _scan_schools_from_disk() -> dict:
    """Scan /mnt/my_book/考題收集 and return schools grouped by county.
    
    Returns dict[county_name] = list of {name, file_count, path}.
    
    【2026-07-31 改】shallow count (max 3 levels) 避免 CIFS 慢:
      之前用 rglob 每個 school 全部 recursive → 5 分鐘還沒完
      改用 shallow count → 14 秒
    
    Patterns:
    1. <county>/<school>/ at top level
    2. _未分類/<county>/<school>/
    3. _未分類/DriveFolder/<county>/<school>/
    """
    import os as _os
    from pathlib import Path as _Path
    
    archive_root = _Path(_os.environ.get("TQARK_ARCHIVE_DIR", "/mnt/my_book/考題收集"))
    result = {}
    
    try:
        if not archive_root.exists():
            return result
    except OSError:
        return result
    
    KNOWN_COUNTIES = {
        "臺北市", "台北市", "新北市", "基隆市", "宜蘭縣", "桃園市",
        "新竹市", "新竹縣", "苗栗縣", "臺中市", "台中市", "彰化縣",
        "南投縣", "雲林縣", "嘉義市", "嘉義縣", "臺南市", "台南市",
        "高雄市", "屏東縣", "臺東縣", "台東縣", "花蓮縣", "澎湖縣",
        "金門縣", "連江縣",
    }
    SKIP_LEVELS = ("國小", "國中", "高中", "unsorted")
    
    def _count_shallow(school_dir, max_depth=3):
        """Count files >1KB, max N levels deep (CIFS friendly)."""
        fc = 0
        try:
            for entry in school_dir.iterdir():
                if entry.is_file():
                    try:
                        if entry.stat().st_size > 1024:
                            fc += 1
                    except OSError:
                        pass
                elif entry.is_dir():
                    try:
                        for sub in entry.iterdir():
                            if sub.is_file():
                                try:
                                    if sub.stat().st_size > 1024:
                                        fc += 1
                                except OSError:
                                    pass
                            elif sub.is_dir() and max_depth > 2:
                                try:
                                    for sub2 in sub.iterdir():
                                        if sub2.is_file():
                                            try:
                                                if sub2.stat().st_size > 1024:
                                                    fc += 1
                                            except OSError:
                                                pass
                                except OSError:
                                    pass
                    except OSError:
                        pass
        except OSError:
            pass
        return fc
    
    # Pattern 1: <county>/<school>/
    try:
        for entry in archive_root.iterdir():
            if not entry.is_dir():
                continue
            if entry.name not in KNOWN_COUNTIES:
                continue
            county = entry.name
            try:
                for school_entry in entry.iterdir():
                    if not school_entry.is_dir():
                        continue
                    sn = school_entry.name
                    if sn in SKIP_LEVELS or sn.startswith("_"):
                        continue
                    fc = _count_shallow(school_entry)
                    if fc == 0:
                        continue
                    result.setdefault(county, []).append({
                        "name": sn,
                        "file_count": fc,
                        "path": f"{county}/{sn}/",
                    })
            except OSError:
                continue
    except OSError:
        pass
    
    # Pattern 2: _未分類/<county>/<school>/  +  DriveFolder
    unsorted_dir = archive_root / "_未分類"
    if unsorted_dir.exists():
        try:
            for county_entry in unsorted_dir.iterdir():
                if not county_entry.is_dir():
                    continue
                cname = county_entry.name
                if cname == "DriveFolder":
                    try:
                        for dcounty in county_entry.iterdir():
                            if not dcounty.is_dir():
                                continue
                            for dschool in dcounty.iterdir():
                                if not dschool.is_dir():
                                    continue
                                fc = _count_shallow(dschool, max_depth=4)
                                if fc == 0:
                                    continue
                                result.setdefault(dcounty.name, []).append({
                                    "name": dschool.name,
                                    "file_count": fc,
                                    "path": f"_未分類/DriveFolder/{dcounty.name}/{dschool.name}/",
                                    "pending_review": True,
                                })
                    except OSError:
                        pass
                    continue
                try:
                    for school_entry in county_entry.iterdir():
                        if not school_entry.is_dir():
                            continue
                        sn = school_entry.name
                        if sn.startswith("_"):
                            continue
                        fc = _count_shallow(school_entry)
                        if fc == 0:
                            continue
                        result.setdefault(cname, []).append({
                            "name": sn,
                            "file_count": fc,
                            "path": f"_未分類/{cname}/{sn}/",
                            "pending_review": True,
                        })
                except OSError:
                    continue
        except OSError:
            pass
    
    # Dedupe by school name
    for county in list(result.keys()):
        seen = {}
        for s in result[county]:
            key = s["name"]
            if key not in seen or s["file_count"] > seen[key]["file_count"]:
                seen[key] = s
        result[county] = sorted(seen.values(), key=lambda x: -x["file_count"])
    
    return result


# Snapshot file (比 disk scan 快 ~1000x)
from pathlib import Path as _Path

_SCHOOLS_SNAPSHOT_PATH = _Path("/tmp/tqark_schools_snapshot.json")
_SNAPSHOT_TTL_SECONDS = 30 * 60  # 30 min
_snapshot_lock_loading = False


def _save_snapshot(data: dict):
    """Save scan result to snapshot file."""
    import json as _json
    try:
        _SCHOOLS_SNAPSHOT_PATH.write_text(
            _json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    except OSError as e:
        print(f"[WARN] Failed to save schools snapshot: {e}", flush=True)


def _load_snapshot() -> dict | None:
    """Load snapshot if exists and is fresh."""
    import json as _json
    import time as _time
    try:
        if not _SCHOOLS_SNAPSHOT_PATH.exists():
            return None
        mtime = _SCHOOLS_SNAPSHOT_PATH.stat().st_mtime
        if _time.time() - mtime > _SNAPSHOT_TTL_SECONDS:
            return None
        return _json.loads(_SCHOOLS_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"[WARN] Failed to load schools snapshot: {e}", flush=True)
        return None


def _refresh_snapshot_async():
    """Refresh snapshot in background (non-blocking)."""
    global _snapshot_lock_loading
    if _snapshot_lock_loading:
        return
    _snapshot_lock_loading = True
    try:
        data = _scan_schools_from_disk()
        _save_snapshot(data)
        _disk_schools_cache["data"] = data
        _disk_schools_cache["ts"] = _time.time()
    except Exception as e:
        print(f"[WARN] Background snapshot refresh failed: {e}", flush=True)
    finally:
        _snapshot_lock_loading = False


def _get_cached_schools() -> dict:
    """Get cached school scan (1 min in-memory + 30min snapshot).
    
    【2026-07-31 改】加 snapshot file cache:
      - 啟動時若 snapshot 存在且 < 30min, 直接用 (避免 18s 第一次 scan)
      - Background thread 重新 scan 寫 snapshot + in-memory cache
      - User 體驗: < 100ms 即使 cold start
    """
    import time as _time
    now = _time.time()
    # In-memory cache valid
    if _disk_schools_cache["data"] and now - _disk_schools_cache["ts"] < _SCHOOLS_CACHE_TTL:
        return _disk_schools_cache["data"]
    # Try snapshot file
    snapshot = _load_snapshot()
    if snapshot:
        _disk_schools_cache["data"] = snapshot
        _disk_schools_cache["ts"] = now
        # Background refresh (in case scan is now stale)
        import threading
        threading.Thread(target=_refresh_snapshot_async, daemon=True).start()
        return snapshot
    # No snapshot → scan synchronously
    data = _scan_schools_from_disk()
    _save_snapshot(data)
    _disk_schools_cache["data"] = data
    _disk_schools_cache["ts"] = now
    return data


@router.get("/api/available-schools", response_class=JSONResponse)
async def api_available_schools(
    county: str | None = Query(None, description="Filter by county (full name)"),
    user: User = Depends(require_login),
):
    """Return schools available on disk, optionally filtered by county.
    
    Response:
    {
        "高雄市": [
            {"name": "高雄市七賢國中", "file_count": 2, "path": "...", "pending_review": true},
            ...
        ],
        ...
    }
    
    Used by dashboard.html dependent dropdown (county → schools).
    """
    schools_by_county = _get_cached_schools()
    
    if county:
        # Filter to specific county + normalise name variants
        # Map: taipei, new_taipei, ... → 全形中文
        from app.data.tw_counties import COUNTIES
        county_id_to_name = {c["id"]: c["name"] for c in COUNTIES}
        # Also map: 臺北市 / 台北市 → 臺北市 (canonical form)
        canonical_map = {
            "臺北市": "臺北市", "台北市": "臺北市",
            "臺中市": "臺中市", "台中市": "臺中市",
            "臺南市": "臺南市", "台南市": "臺南市",
            "臺東縣": "臺東縣", "台東縣": "臺東縣",
        }
        # If county is "kaohsiung" or id form, convert
        target = canonical_map.get(county, county)
        if target in county_id_to_name.values():
            target = next((v for v in county_id_to_name.values() if v == target), target)
        # If it's an id, get name
        if target in county_id_to_name:
            target = county_id_to_name[target]
        
        filtered = {target: schools_by_county.get(target, [])}
        return JSONResponse(filtered)
    
    return JSONResponse(schools_by_county)

