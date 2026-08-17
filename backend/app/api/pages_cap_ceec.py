"""
CAP / CEEC 統一考試 page routes (2026-08-17 新 Pages Split 2).

Provides routes for 歷屆會考 (CAP) + 大學入學考 (CEEC) 統一考試:
- /ui/cap-exam  (GET)         → 瀏覽 CAP 考題
- /ui/cap-exam/download/{rel:path} → 下載 CAP PDF
- /ui/ceec-exam (GET)         → 瀏覽 CEEC 考題
- /ui/ceec-exam/download/{rel:path} → 下載 CEEC PDF

內含 helper functions:
- _parse_cap_filename / _parse_ceec_filename: parse 統一考試 PDF 檔名
- _match_ceec_subject / _normalize_ceec_subject: CEEC subject 標準化
- _check_invalidate_signal / _refresh_pdf_tree_bg: PDF tree cache
- _do_scan_pdf_tree / _scan_pdf_tree: scan 統一考試資料夾

【拆分原則】
- 統一考試 (CAP/CEEC) 是獨立功能, 不混進 StudyArk
- 從 pages.py 抽出, pages.py 仍管 landing / dashboard / search / admin / batch_download
- 共用 helpers (_user_ctx / _common_ctx) 從 pages_helpers import
"""
from pathlib import Path
import os
import time as _time
import threading
from datetime import datetime, timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.pages_helpers import (
    _user_ctx,
    _common_ctx,
    templates,
    _permission_name,
)
from app.db.models import User
from app.db.session import get_db
from app.core.deps import require_login, get_current_user_from_token

CAP_DIR = Path("/mnt/my_book/考題收集/cap_exam")
CEEC_DIR = Path("/mnt/my_book/考題收集/ceec")

# 【2026-07-25 新】CAP/CEEC 結果每頁顯示數
EXAM_ITEMS_PER_PAGE = 15

cap_ceec_router = APIRouter(tags=["cap_ceec"])

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


@cap_ceec_router.get("/ui/cap-exam", response_class=HTMLResponse)
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

    # 【2026-08-17 新】filter chips 用對 (template 個別移除按鈕 URL rebuild)
    params_display = {
        k: v for k, v in [
            ("grade", "會考"),  # 固定標記, 不影響 SQL (CAP page 永遠是會考)
            ("school_year", year if year else ""),
            ("subject", subject or ""),
            ("filetype", filetype or ""),
        ] if v
    }
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
            "params_display": params_display,
            "deploy_ts": int(os.path.getmtime(Path(__file__).parent.parent / "static" / "cache_check.js")),
            "total_files": total,
            "total_size": sum(i["size"] for i in items),
            "page": page,
            "total_page": total_page,
            "per_page": EXAM_ITEMS_PER_PAGE,
            "qs_for_page": qs_for_page,
        },
    )


@cap_ceec_router.get("/ui/cap-exam/download/{rel:path}")
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


@cap_ceec_router.get("/ui/ceec-exam", response_class=HTMLResponse)
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

    # 【2026-08-17 新】filter chips 用對 (template 個別移除按鈕 URL rebuild)
    params_display = {
        k: v for k, v in [
            ("grade", "大學入學考"),  # 固定標記, 不影響 SQL (CEEC page 永遠是大學入學考)
            ("school_year", year if year else ""),
            ("exam_type", exam_type or ""),
            ("subject", subject or ""),
            ("filetype", filetype or ""),
        ] if v
    }
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
            "params_display": params_display,
            "deploy_ts": int(os.path.getmtime(Path(__file__).parent.parent / "static" / "cache_check.js")),
            "total_files": total,
            "total_size": sum(i["size"] for i in items),
            "page": page,
            "total_page": total_page,
            "per_page": EXAM_ITEMS_PER_PAGE,
            "qs_for_page": qs_for_page,
        },
    )


@cap_ceec_router.get("/ui/ceec-exam/download/{rel:path}")
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
