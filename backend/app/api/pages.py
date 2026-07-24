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
from fastapi.responses import HTMLResponse, FileResponse, Response, RedirectResponse
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


def _common_ctx(user: User | None) -> dict:
    return {
        "user": _user_ctx(user),
        "version": "0.1.2",
    }


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

    return templates.TemplateResponse(
        "landing.html",
        {**_common_ctx(user), "request": request, "stats": stats},
    )


# === Archive PDF count cache (2026-07-24) ===
# 因為 /mnt/my_book 是 CIFS 網路磁碟, rglob 超慢
# In-memory cache + 10 分鐘 TTL, background refresh
_archive_counts_cache: dict = {"data": None, "ts": 0.0}
_archive_counts_lock = threading.Lock()
_ARCHIVE_COUNTS_TTL = 600  # 10 minutes


def _get_cached_archive_counts() -> dict:
    """Get cached PDF counts. Returns previous cache if still fresh."""
    now = _time.time()
    with _archive_counts_lock:
        if _archive_counts_cache["data"] is not None and now - _archive_counts_cache["ts"] < _ARCHIVE_COUNTS_TTL:
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

    Return keys: count_elementary, count_junior, count_senior, count_cap, count_ceec, total_all
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
    }
    if not archive_root.exists():
        return result

    SKIP_TOP_DIRS = ("state", "logs")  # cap_exam/ceec 還是要 walk

    count_elementary = 0
    count_junior = 0
    count_senior = 0
    count_cap = 0
    count_ceec = 0

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
                if not fname.endswith(".pdf"):
                    continue
                full = os.path.join(dirpath, fname)
                rel = os.path.relpath(full, archive_root)
                parts = rel.split(os.sep)

                # Top-level dirs: cap_exam, ceec
                if parts and parts[0] == "cap_exam":
                    count_cap += 1
                    continue
                if parts and parts[0] == "ceec":
                    count_ceec += 1
                    continue
                # 其他縣市路徑檢查 "國小"/"國中"/"高中"
                # 格式: <county>/<level>/<grade>/<subject>/<filetype>/file.pdf
                # 或: <level>/<grade>/<subject>/<filetype>/file.pdf (未分 county)
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
    except OSError:
        pass

    result["count_elementary"] = count_elementary
    result["count_junior"] = count_junior
    result["count_senior"] = count_senior
    result["count_cap"] = count_cap
    result["count_ceec"] = count_ceec
    result["total_all"] = (
        count_elementary + count_junior + count_senior + count_cap + count_ceec
    )
    return result


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

    return templates.TemplateResponse(
        "dashboard.html",
        {
            **_common_ctx(user),
            "request": request,
            "user_full": user,  # 完整 User object 給 template 用 datetime 等
            "cap_subjects_json": json.dumps(cap_subjects, ensure_ascii=False),
            "ceec_subjects_json": json.dumps(ceec_subjects, ensure_ascii=False),
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
# 解法: in-memory cache + TTL + background refresh
#   - cache hit (TTL 內): 瞬間 return
#   - stale (TTL 外): return stale + background thread refresh
#   - 10 分鐘 TTL: archive cron 加新檔後 10 分鐘內看不到 (可接受, 因為 subject filter 對 user 主動加檔不敏感)
#
# 不需要建 db: 全部 metadata 只 ~300KB in-memory
_pdf_tree_cache: dict[str, tuple[float, list[dict]]] = {}
_pdf_tree_lock = threading.Lock()
_PDF_TREE_TTL = 600  # 10 minutes


def _refresh_pdf_tree_bg(root_str: str, root: Path):
    """Background refresh, non-blocking"""
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

    【2026-07-24】in-memory cache + TTL + background refresh
    避免每次 request 都 rglob 一次 (CEEC 1490 PDFs 要 9 秒)
    - TTL 10 分鐘, stale 時 return 舊 cache + background thread refresh
    - cold start (cache 空) 時同步 scan, 之後的 request 都是瞬時
    """
    root_str = str(root)
    now = _time.time()

    cached = _pdf_tree_cache.get(root_str)
    if cached is not None:
        cached_ts, cached_items = cached
        if now - cached_ts < _PDF_TREE_TTL:
            return cached_items  # Fresh cache hit
        # Stale: return old + background refresh
        import threading as _threading
        _threading.Thread(target=_refresh_pdf_tree_bg, args=(root_str, root), daemon=True).start()
        return cached_items

    # Cold start: synchronous scan
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
):
    """
    歷屆國中教育會考瀏覽頁面 (CAP / RCPET)。
    公開頁面,不需登入 (但下載連結在 archive 路徑,Web UI 只列出 metadata)。

    【2026-07-24 新】支援 subject / filetype 篩選 (從 dashboard form 送過來)
    """
    return _render_cap_exam_results(request, user, year, subject, filetype)


def _render_cap_exam_results(request, user, year, subject, filetype):
    """
    內部 helper: 渲染 cap_exam.html 結果。可由 cap_exam_browser 或 /ui/search (grade=會考) 呼。
    """
    # 取 raw items, 做 filter (subject + filetype + year)
    # 【2026-07-24 改】一個 call _scan_pdf_tree() 就好, 避免 repeated scan
    all_items = _scan_pdf_tree(CAP_DIR)

    # Apply subject/filetype filters
    items = all_items
    if subject:
        items = [i for i in items if i["subject"] == subject]
    if filetype:
        items = [i for i in items if filetype in i["file_type"]]

    # Group by year
    by_year: dict[int, list] = {}
    for item in items:
        by_year.setdefault(item["year"], []).append(item)

    # Apply year filter
    if year is not None:
        by_year = {year: by_year.get(year, [])}

    # Build year/subject lists (for filter UI) - 用未 filter 的 all_items
    all_years = sorted(set(i["year"] for i in all_items if i["year"] > 0), reverse=True)
    all_subjects = sorted(set(i["subject"] for i in all_items if i["subject"]))

    return templates.TemplateResponse(
        "cap_exam.html",
        {
            **_common_ctx(user),
            "request": request,
            "by_year": dict(sorted(by_year.items(), reverse=True)),
            "all_years": all_years,
            "all_subjects": all_subjects,
            "selected_year": year,
            "selected_subject": subject,
            "selected_filetype": filetype,
            "total_files": len(items),
            "total_size": sum(i["size"] for i in items),
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
):
    """
    歷屆大學入學考試瀏覽頁面 (CEEC)。
    公開頁面 (metadata),下載要登入。

    【2026-07-24 新】支援 subject / filetype 篩選 (從 dashboard form 送過來)
    """
    return _render_ceec_exam_results(request, user, exam_type, year, subject, filetype)


def _render_ceec_exam_results(request, user, exam_type, year, subject, filetype):
    """
    內部 helper: 渲染 ceec_exam.html 結果。可由 ceec_exam_browser 或 /ui/search (grade=大學入學考) 呼。
    """
    # 【2026-07-24 改】一次取 all_items, 用全量建 filter buttons (受 cache 保護, 0.02s)
    all_items = _scan_pdf_tree(CEEC_DIR)

    # Apply subject/filetype filters
    items = all_items
    if subject:
        items = [i for i in items if i["subject"] == subject]
    if filetype:
        items = [i for i in items if filetype in i["file_type"]]

    # Group by (exam_type, year)
    grouped: dict[tuple, list] = {}
    for item in items:
        key = (item["exam_type"], item["year"])
        grouped.setdefault(key, []).append(item)

    # Apply filters
    if exam_type is not None:
        grouped = {k: v for k, v in grouped.items() if k[0] == exam_type}
    if year is not None:
        grouped = {k: v for k, v in grouped.items() if k[1] == year}

    # Sorted
    grouped = dict(sorted(grouped.items(), reverse=True))

    # Build filter lists (from all_items, 不受 subject filter 影響)
    all_exam_types = sorted(set(i["exam_type"] for i in all_items if i["exam_type"]))
    all_years = sorted(set(i["year"] for i in all_items if i["year"] > 0), reverse=True)
    all_subjects = sorted(set(i["subject"] for i in all_items if i["subject"]))

    return templates.TemplateResponse(
        "ceec_exam.html",
        {
            **_common_ctx(user),
            "request": request,
            "grouped": grouped,
            "all_exam_types": all_exam_types,
            "all_years": all_years,
            "all_subjects": all_subjects,
            "selected_exam_type": exam_type,
            "selected_year": year,
            "selected_subject": subject,
            "selected_filetype": filetype,
            "total_files": len(items),
            "total_size": sum(i["size"] for i in items),
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
        return _render_cap_exam_results(request, user, year, subject, filetype)
    if grade == "大學入學考":
        year = int(school_year) if school_year and school_year.isdigit() else None
        filetype = daan if daan and daan in ("yes", "no") else None
        return _render_ceec_exam_results(request, user, None, year, subject, filetype)

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
        return _render_cap_exam_results(request, user, year, subject, filetype)
    if grade == "大學入學考":
        year = int(school_year) if school_year and school_year.isdigit() else None
        filetype = daan if daan and daan in ("yes", "no") else None
        return _render_ceec_exam_results(request, user, None, year, subject, filetype)

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