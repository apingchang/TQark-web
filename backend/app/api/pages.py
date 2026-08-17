"""
HTML page routes

- GET /         → landing page
- GET /dashboard → user dashboard
- GET /admin    → admin panel
- GET /static/*  → 靜態檔案
"""

import os
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
from app.api.pages_helpers import (
    TEMPLATES_DIR,
    STATIC_DIR,
    templates,  # 【2026-08-17 新】共用 templates instance
    _background_fetch_companion,
    _user_ctx,
    _common_ctx,
    _get_build_mtime,
    _permission_name,
    PERMISSION_NAMES,
)
# 【2026-08-17 Pages Split 2】CAP/CEEC consts 從 pages_cap_ceec.py import
from app.api.pages_cap_ceec import (
    CAP_DIR,
    CEEC_DIR,
    _scan_pdf_tree,
    _render_cap_exam_results,    # 【2026-08-17】alias 給既有 tests
    _render_ceec_exam_results,    # 【2026-08-17】alias 給既有 tests
    cap_ceec_router,
)
# 【2026-08-17 Pages Split 3】search routes + helpers 從 pages_search.py
from app.api.pages_search import (
    search_router,
    _scan_schools_from_disk,    # 【2026-08-17】alias 給既有 tests
    _get_cached_schools,         # 【2026-08-17】alias 給既有 tests
)


router = APIRouter(tags=["pages"])

# 【2026-08-17 Pages Split 2】include cap/ceec routes (從 pages_cap_ceec.py)
router.include_router(cap_ceec_router)

# 【2026-08-17 Pages Split 3】include search routes (從 pages_search.py)
router.include_router(search_router)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"



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
_archive_counts_cache: dict = {"data": None, "ts": 0.0, "scanning": False}
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
    """Get cached PDF counts. Returns previous cache if still fresh.

    【2026-08-03 新】return dict 多加 cache_age (人類可讀字串, 給 template footer 用):
      - "剛剛更新" (< 1 分鐘)
      - "X 分鐘前" (< 1 小時)
      - "X 小時前" (< 1 天)
      - "X 天前"
    """
    now = _time.time()
    # 【2026-07-28】如果 archive 正在跑 (log 5 分鐘內改過), 視為 stale
    recent_activity = _has_recent_archive_activity()
    with _archive_counts_lock:
        if _archive_counts_cache["data"] is not None and now - _archive_counts_cache["ts"] < _ARCHIVE_COUNTS_TTL and not recent_activity:
            data = dict(_archive_counts_cache["data"])
            data["cache_age"] = _format_cache_age(now - _archive_counts_cache["ts"])
            return data
        if _archive_counts_cache["data"] is not None:
            # Stale — trigger background refresh (non-blocking), return stale
            import threading as _threading
            _threading.Thread(target=_refresh_archive_counts_bg, daemon=True).start()
            data = dict(_archive_counts_cache["data"])
            data["cache_age"] = _format_cache_age(now - _archive_counts_cache["ts"]) + " (背景更新中)"
            return data
    # Cold start or cache missing — 【2026-08-07 改】background scan + placeholder, 不再 block request
    with _archive_counts_lock:
        if not _archive_counts_cache["scanning"]:
            _archive_counts_cache["scanning"] = True
            import threading as _threading
            def _bg_cold_scan():
                try:
                    _refresh_archive_counts_bg()
                finally:
                    with _archive_counts_lock:
                        _archive_counts_cache["scanning"] = False
            _threading.Thread(target=_bg_cold_scan, daemon=True, name="cold-scan-archive-counts").start()
    # Return placeholder (讓 user 看到 dashboard 立刻 render; 之後 reload 才看到真實數字)
    data = {
        "count_elementary": 0, "count_junior": 0, "count_senior": 0,
        "count_cap": 0, "count_ceec": 0, "total_all": 0, "count_tcool": 0,
        "by_county": {}, "by_school": {}, "by_subject": {},
        "latest_files": [], "archive_size_mb": 0, "total_files": 0,
    }
    data["cache_age"] = "首次掃描中 (背景跑, 不阻塞頁面)"
    return data


def _format_cache_age(seconds: float) -> str:
    """Format seconds elapsed as human-readable string."""
    if seconds < 60:
        return "剛剛更新"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分鐘前"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小時前"
    return f"{int(seconds // 86400)} 天前"


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

    SKIP_TOP_DIRS = ("state", "logs", "_inbox", "_internal", "_未分類", "_待分類", "其他X", "未分類")  # cap_exam/ceec 還是要 walk; 2026-08-07 加 _inbox/_internal (folder 整理產物)

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
    cap_items = _scan_pdf_tree(CAP_DIR)
    ceec_items = _scan_pdf_tree(CEEC_DIR)
    cap_subjects = sorted({i["subject"] for i in cap_items if i["subject"]})
    ceec_subjects = sorted({i["subject"] for i in ceec_items if i["subject"]})
    # 【2026-08-17 新】CAP/CEEC 真實年度清單 (dashboard 切 mode 時填 datalist)
    cap_years = sorted({i["year"] for i in cap_items if i["year"] > 0}, reverse=True)
    ceec_years = sorted({i["year"] for i in ceec_items if i["year"] > 0}, reverse=True)
    # 【2026-08-17 新】StudyArk 真實學年 (從 DB 拿, 不再 hardcode 83-115)
    #   DB 欄位 school_year, 排除 DriveFolder + CAP/CEEC 資料夾
    #   【2026-08-17 修】用 with 確保 conn 正確 close (避免 sqlite3.ProgrammingError)
    from app.scraper import db as _db_mod
    # 【2026-08-17 修】用 _connect() 但不要 close (cached connection)
    _conn = _db_mod._connect()
    _studyark_years_rows = _conn.execute("""
        SELECT DISTINCT school_year FROM files
        WHERE school_year != '' AND school_year IS NOT NULL
          AND (rel_path NOT LIKE '%_drivefolder/%')
          AND (rel_path NOT LIKE '%/cap_exam/%')
          AND (rel_path NOT LIKE '%/ceec/%')
        ORDER BY CAST(school_year AS INTEGER) DESC
    """).fetchall()
    studyark_years = [int(r[0]) for r in _studyark_years_rows if r[0] and r[0].isdigit()]
    print(f'[dashboard] StudyArk 真實學年 ({len(studyark_years)}): {studyark_years[:5]}...{studyark_years[-3:]}', flush=True)

    # 【2026-07-28 移】平台資訊搬到 dashboard.html
    stats = _get_cached_archive_counts()

    # 【2026-08-17 新】random nonce (每次 request 不同, 防止 proxy cache HTML)
    import secrets as _secrets
    _nonce = _secrets.token_urlsafe(8)
    response = templates.TemplateResponse(
        "dashboard.html",
        {
            **_common_ctx(user),
            "request": request,
            "user_full": user,
            "cap_subjects_json": json.dumps(cap_subjects, ensure_ascii=False),
            "ceec_subjects_json": json.dumps(ceec_subjects, ensure_ascii=False),
            "cap_years_json": json.dumps(cap_years, ensure_ascii=False),
            "ceec_years_json": json.dumps(ceec_years, ensure_ascii=False),
            "studyark_years": studyark_years,
            "stats": stats,
            "deploy_ts": int(os.path.getmtime(Path(__file__).parent.parent / "static" / "dashboard_form.js")),
            "nonce": _nonce,
        },
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


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

    # 【2026-08-03 新】考題統計 (右側 sidebar 顯示考題資訊需要)
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
            "stats": _get_cached_archive_counts(),
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
# - POST /api/tools/jp-upload               (multipart: file + engine → 背景)
# - GET  /api/tools/jp-download?filename=... (下載翻譯結果, browser Save As)
# - GET  /api/tools/jp-status                (查 status, 前端 1.5s poll)
# - POST /api/tools/jp-reset                 (重置)

@router.post("/api/tools/jp-upload")
async def jp_upload(
    request: Request,
    user: User = Depends(require_approved),
):
    """【2026-08-01 新】接收上傳 .docx + 啟動翻譯 (multipart, 限 perm 0/1)
    
    Form fields:
        file: .docx
        engine: google | minimax | all
    
    Returns:
        {ok, task_id, filename, engine, staging_path}
    """
    if not user or user.permission is None or user.permission > 1:
        raise HTTPException(403, "🔒 此 API 僅限家人/管理員使用")
    
    from fastapi import UploadFile, File, Form
    from app.tools.jp_translator import save_uploaded_file, start_translation
    
    form = await request.form()
    file = form.get("file")
    engine = form.get("engine", "all")
    
    if not file or not hasattr(file, "filename"):
        raise HTTPException(400, "file 必填 (multipart/form-data)")
    
    filename = file.filename
    if not filename or not filename.endswith(".docx"):
        raise HTTPException(400, f"只接受 .docx, 收到: {filename}")
    
    if engine not in ("google", "minimax", "all"):
        raise HTTPException(400, f"engine 必須是 google / minimax / all, 收到: {engine}")
    
    # Read + save to staging
    contents = await file.read()
    if len(contents) == 0:
        raise HTTPException(400, "file 是空的")
    if len(contents) > 50 * 1024 * 1024:  # 50MB limit
        raise HTTPException(400, f"file 太大 ({len(contents)/1024/1024:.1f}MB, 上限 50MB)")
    
    staging_path = save_uploaded_file(contents, filename)
    
    # Start translation (background)
    result = start_translation(str(staging_path), filename, engine)
    if not result.get("ok"):
        # Cleanup staging
        try:
            staging_path.unlink()
        except Exception:
            pass
        raise HTTPException(409, result.get("error", "啟動失敗"))
    
    result["staging_path"] = str(staging_path)
    return result


@router.get("/api/tools/jp-download/{task_id}/{engine}")
async def jp_download(
    task_id: str,
    engine: str,  # google or minimax
    user: User = Depends(require_approved),
):
    """【2026-08-01 新】下載翻譯結果 .docx (限 perm 0/1)
    
    Args:
        task_id: 任務 id (從 status 拿)
        engine: google / minimax
    
    Returns:
        FileResponse with Content-Disposition: attachment (browser 觸發 Save As)
    """
    if not user or user.permission is None or user.permission > 1:
        raise HTTPException(403, "🔒 此 API 僅限家人/管理員使用")
    
    if engine not in ("google", "minimax"):
        raise HTTPException(400, f"engine 必須是 google / minimax, 收到: {engine}")
    
    from fastapi.responses import FileResponse
    from app.tools.jp_translator import get_status
    
    # Get status to find filename
    status = get_status()
    if status.get("task_id") != task_id:
        raise HTTPException(404, f"Task {task_id} 不存在或已過期")
    if status.get("state") != "done":
        raise HTTPException(400, f"Task 還沒完成 (state={status.get('state')})")
    
    # Find output file by engine suffix
    output_files = status.get("output_files", [])
    target_file = None
    for f in output_files:
        if f.endswith(f"_{engine}.docx"):
            target_file = f
            break
    
    if not target_file or not Path(target_file).exists():
        raise HTTPException(404, f"找不到 {engine} output file")
    
    return FileResponse(
        path=target_file,
        filename=Path(target_file).name,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


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


# 【2026-08-17 Pages Split 3】
# _disk_schools_cache / _SCHOOLS_CACHE_TTL / _SNAPSHOT_TTL_SECONDS / _SCHOOLS_SNAPSHOT_PATH
# 都搬到 app/api/pages_search.py (由 search routes 使用)
# pages.py 不再需要
