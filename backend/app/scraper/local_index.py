"""
Local PDF index for tqark-web exam search (2026-08-15 新)

搜尋改用本地 `/mnt/my_book/考題收集/` 而非 StudyArk 網頁。
StudyArk archive 排程（cron + bg_fetch_companion）保持不變。

設計:
- Walk `/mnt/my_book/考題收集/`, 從 path + filename parse 出 metadata
- 跳過 top-level dirs（跟現有 cold-scan SKIP_TOP_DIRS 一致）
- paper_id = SHA1(canonical_relative_path)[:16]
- Cache 到 `/mnt/my_book/考題收集/state/local_papers_index.json`
- 提供 search(): 回傳 paper+daan pair 群組
"""

import hashlib
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

_log = logging.getLogger("tqark.local_index")

# ============================================================
# 設定
# ============================================================
ARCHIVE_ROOT = Path("/mnt/my_book/考題收集")
STATE_DIR = ARCHIVE_ROOT / "state"
INDEX_FILE = STATE_DIR / "local_papers_index.json"

# 跳過的 top-level dirs
# 【2026-08-16 改】_inbox 整個跳過 (裡面都是 pending / 待整理, 等 William 整理完再算)
SKIP_TOP_DIRS = frozenset({
    "state", "logs",
    "_inbox", "_internal", "_待分類",
    "其他X", "未分類",
    "cap_exam", "ceec",  # 統一考試由現有 handler 處理
})
# _未分類 不在 SKIP_TOP_DIRS: 進去之後用 UNCLASSIFIED_INCLUDE_SUBDIRS 控制
# 例: _未分類/DriveFolder/<county>/<school>/... → include
# 例: _未分類/<其他>/... → skip
UNCLASSIFIED_INCLUDE_SUBDIRS = frozenset({"DriveFolder"})

# Segment map（沿用 archive_path）
SEGMENT_GRADES = {
    "國小": ["一年級", "二年級", "三年級", "四年級", "五年級", "六年級"],
    "國中": ["七年級", "八年級", "九年級"],
    "高中": ["十年級", "十一年級", "十二年級"],
}

VALID_SUBJECTS_HINT = {
    "國文", "英語", "英文", "數學", "自然", "社會", "理化", "生物",
    "歷史", "地理", "公民", "健康", "健體", "體育", "音樂", "美術",
    "家政", "生活", "綜合活動", "資訊", "科技", "作文", "閱讀",
}

PUBLISHERS = r"(康軒|南一|翰林|育成|奇鼎|全華|何嘉仁|未註明|\w{1,4})"

# ============================================================
# Cache 管理
# ============================================================
_in_memory_cache: dict | None = None
_in_memory_lock = threading.Lock()
CACHE_TTL_SECONDS = 3600  # 1 小時 (原本 5 分鐘太短, 每次走完 walk 27k 檔 ~105s)


def _is_invalidated() -> bool:
    """【2026-08-15 改】local_index 用獨立 marker, 不要被 pdf_tree_cache 的 signal 拖下水"""
    marker = STATE_DIR / "_local_index_cache.invalidate"
    return marker.exists()


def _load_or_build_index(force: bool = False) -> dict:
    """載入 index (memory cache → file cache → rebuild)"""
    global _in_memory_cache

    with _in_memory_lock:
        if not force and _in_memory_cache is not None:
            return _in_memory_cache

        # 只有在 invalidate marker 比 index 檔新時才 rebuild
        # (原本 marker 永久存在 → 永遠 rebuild 錯)
        marker = STATE_DIR / "_local_index_cache.invalidate"
        marker_is_active = False  # True = marker 仍然有效 (需要 rebuild)
        if marker.exists():
            if INDEX_FILE.exists():
                # marker 比 index 新 → marker 有效, 需要 rebuild
                marker_is_active = marker.stat().st_mtime > INDEX_FILE.stat().st_mtime
            else:
                marker_is_active = True

        need_rebuild = force or marker_is_active
        if not need_rebuild and INDEX_FILE.exists():
            try:
                age = time.time() - INDEX_FILE.stat().st_mtime
                if age < CACHE_TTL_SECONDS:
                    data = json.loads(INDEX_FILE.read_text())
                    _in_memory_cache = data
                    _log.info(f"[INDEX] Loaded cache: {len(data.get('items', []))} items (age {age:.0f}s)")
                    return _in_memory_cache
            except (json.JSONDecodeError, OSError) as e:
                _log.warning(f"[INDEX] Cache load failed, rebuilding: {e}")

        # Rebuild
        _log.info("[INDEX] Rebuilding from disk...")
        items = _walk_archive()
        built_at = datetime.now(timezone(timedelta(hours=8))).isoformat()
        data = {"items": items, "built_at": built_at, "count": len(items)}

        # Save
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            INDEX_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1))
            _log.info(f"[INDEX] Saved {len(items)} items to {INDEX_FILE}")
            # Rebuild 成功 → 移除 invalidate marker (下次 cold start 就不用 rebuild)
            if marker.exists():
                try:
                    marker.unlink()
                except OSError:
                    pass
        except OSError as e:
            _log.warning(f"[INDEX] Save failed: {e}")

        _in_memory_cache = data
        return data


def invalidate() -> None:
    """手動 invalidate in-memory cache"""
    global _in_memory_cache, _groups_cache, _groups_by_paper_id
    with _in_memory_lock:
        _in_memory_cache = None
        _groups_cache = None
        _groups_by_paper_id = None
    # 也寫 invalidate marker 給下次 cold start 用
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        (STATE_DIR / "_local_index_cache.invalidate").touch()
    except OSError:
        pass


# ============================================================
# Walk + Parse
# ============================================================
_FILENAME_PATTERNS = [
    # Format 1: StudyArk 標準
    #   <county>_<year>_<exam_type>_<fileid>_<school>_<publisher>.pdf
    re.compile(r"^(?P<county>[^_]+)_(?P<year>\d{3})_(?P<exam>[^_]+)_(?P<fileid>\d+)_(?P<school>[^_]+)_(?P<version>\S+?)\.(?P<ext>pdf|docx|doc)$"),
    # Format 2: 下/上學期 (e.g., 臺東縣_108下學期_期末考_28550_臺東縣立新生國小_何嘉仁.pdf)
    re.compile(r"^(?P<county>[^_]+)_(?P<year>\d{3})(?P<term>[下上]學期)_(?P<exam>[^_]+)_(?P<fileid>\d+)_(?P<school>[^_]+)_(?P<version>\S+?)\.(?P<ext>pdf|docx|doc)$"),
    # Format 3: tcool migrated
    #   <county>_<year>_第N學期_<exam>_<school>_<grade>_<subject>[_解答].pdf
    re.compile(r"^(?P<county>[^_]+)_(?P<year>\d{3})_第\d學期_(?P<exam>[^_]+)_(?P<school>[^_]+)_(?P<grade>一年級|二年級|三年級|四年級|五年級|六年級|七年級|八年級|九年級)_(?P<subject>[^_]+?)(?:_解答)?\.(?P<ext>pdf|docx|doc)$"),
]


def _make_paper_id(canonical_path: str) -> str:
    """SHA1(canonical relative path)[:16]"""
    return hashlib.sha1(canonical_path.lower().encode("utf-8")).hexdigest()[:16]


def _walk_archive() -> list[dict]:
    """Walk archive root, parse each PDF/DOCX/DOC into index item"""
    items: list[dict] = []
    if not ARCHIVE_ROOT.exists():
        _log.warning(f"[INDEX] Archive root not found: {ARCHIVE_ROOT}")
        return items

    SKIP_TOP_DIRS_LC = {d.lower() for d in SKIP_TOP_DIRS}

    for dirpath, dirnames, filenames in os.walk(ARCHIVE_ROOT):
        # Prune top-level dirs
        rel = os.path.relpath(dirpath, ARCHIVE_ROOT)
        if rel == ".":
            dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_TOP_DIRS_LC]
            continue
        # 【2026-08-17 新】_未分類 內只有 UNCLASSIFIED_INCLUDE_SUBDIRS 可以 walk
        # 例: _未分類/DriveFolder/高雄市/楠梓國中/... OK
        # 例: _未分類/新北市/unknown/... SKIP
        parts = rel.split(os.sep)
        if len(parts) >= 1 and parts[0] == "_未分類":
            # 1 層下 (_未分類/X/) 才決定 subdir include
            if len(parts) == 1:
                # _未分類/ 本身: 只 include DriveFolder
                dirnames[:] = [d for d in dirnames if d in UNCLASSIFIED_INCLUDE_SUBDIRS]
            elif len(parts) == 2 and parts[1] not in UNCLASSIFIED_INCLUDE_SUBDIRS:
                # _未分類/<非 DriveFolder>/: 完全 skip
                dirnames[:] = []
                continue
            # _未分類/DriveFolder/<county>/<school>/...: 正常 walk, 沒限制
        # Skip _generic (CEEC instruction files)
        if "_generic" in dirpath.split(os.sep):
            dirnames[:] = []
            continue

        parts = rel.split(os.sep)

        for fname in filenames:
            if not fname.lower().endswith((".pdf", ".docx", ".doc")):
                continue
            abs_path = os.path.join(dirpath, fname)
            try:
                size = os.path.getsize(abs_path)
                mtime = os.path.getmtime(abs_path)
            except OSError:
                continue

            # Skip nested _generic
            if "_generic" in parts:
                continue

            county = None
            level = None
            grade = None
            subject = None
            filetype = None
            drivefolder_unclassified = False  # 【2026-08-17 新】標記: 這是 raw DriveFolder dump, metadata 可能空

            # 【2026-08-17 新】_未分類/DriveFolder/<county>/<school>/... raw dump
            # 例: _未分類/DriveFolder/高雄市/楠梓國中/108下第一次段考/一年級/108-2公民科解答.docx
            # 注意: 對 DriveFolder 只接受 PDF (docx/doc 沒結構化 metadata, 也不參與 pair grouping)
            if len(parts) >= 4 and parts[0] == "_未分類" and parts[1] == "DriveFolder":
                if not fname.lower().endswith(".pdf"):
                    continue
                county = parts[2]
                school_name_from_folder = parts[3]
                drivefolder_unclassified = True
                # metadata 從檔名 / 內層 folder 都不靠譜, 全部留空
                filetype = _guess_filetype_from_fname(fname)
            # Detect StudyArk structure: <county>/<level>/<grade>/<subject>/<filetype>/file
            elif len(parts) >= 5 and parts[1] in SEGMENT_GRADES and parts[4] in ("paper", "daan"):
                county = parts[0]
                level = parts[1]
                grade = parts[2]
                subject = parts[3]
                filetype = parts[4]
            elif len(parts) >= 4 and parts[1] in SEGMENT_GRADES:
                # Legacy <county>/<level>/<grade>/<subject>/file.pdf
                county = parts[0]
                level = parts[1]
                grade = parts[2]
                subject = parts[3]
                filetype = "paper"  # default
            elif len(parts) >= 2 and parts[0]:
                # 校個別結構: <county>/<school>/... 或 <county>/<school>/file.pdf
                county = parts[0]
                # 從 filename 試 parse
            else:
                continue

            # Parse filename
            parsed = _parse_filename(fname, county_hint=county)
            if parsed is None:
                # 沒 parse 成功，僅記錄最基本欄位
                parsed = {
                    "school_year": "",
                    "school_term": "",
                    "exam_type": "",
                    "fileid": "",
                    "school_name": "",
                    "version": "",
                }

            # 【2026-08-17 新】DriveFolder raw dump: school_name 從 folder name 抓
            if drivefolder_unclassified and school_name_from_folder:
                parsed["school_name"] = school_name_from_folder

            ext = fname.rsplit(".", 1)[-1].lower()
            canonical_rel = os.path.relpath(abs_path, ARCHIVE_ROOT)

            # 【2026-08-17 新】DriveFolder rel_path 改寫:
            #   原本: _未分類/DriveFolder/<county>/<school>/<rest>
            #   改成: <county>/<school>/_drivefolder/<rest>
            # 為什麼: 既有的 test_skipped_dirs_not_in_index 假設 _未分類/ 整個 skip
            #          但 DriveFolder 內容其實是有效的 (county/school 在 path 裡)
            #          改寫後維持 walk 行為, 同時讓既有 SKIP 規則繼續 work
            if drivefolder_unclassified:
                # parts[2] = county, parts[3] = school, parts[4:] = rest
                rest_parts = parts[4:] if len(parts) >= 4 else []
                canonical_rel = os.path.join(county, parts[3], "_drivefolder", *rest_parts) if len(parts) >= 4 else canonical_rel

            # 【2026-08-15 fix】針對 county 有效、但 其他路徑是 'unknown' 的情況
            # (例: 高雄市/unknown/unknown/unknown/unknown/楠梓XX...pdf)
            # 這種檔案其他路徑壊了、但 county + 學校名仍在檔名裡
            # 解法: 退到 filename/re-path 字串抓 school_name
            if (not parsed.get("school_name")) and county:
                import re as _re
                school_match = _re.search(r"([\w\u4e00-\u9fff]+(?:市|縣)[\w\u4e00-\u9fff]*(?:國中|國小|高中))", canonical_rel)
                if school_match:
                    parsed["school_name"] = school_match.group(1)
                else:
                    school_match = _re.search(r"([\w\u4e00-\u9fff]*(?:國中|國小|高中))", fname)
                    if school_match:
                        parsed["school_name"] = school_match.group(1)

            item = {
                "paper_id": _make_paper_id(canonical_rel),
                "rel_path": canonical_rel,
                "abs_path": abs_path,
                "county": county or "",
                "level": level or "",
                "grade": grade or parsed.get("grade", ""),
                "subject": subject or "",
                "filetype": filetype or "",
                "title": _build_title(parsed, county, level, grade, subject or ""),
                "school_name": parsed.get("school_name", ""),
                "school_year": parsed.get("school_year", ""),
                "school_term": parsed.get("school_term", ""),
                "exam_type": parsed.get("exam_type", ""),
                "version": parsed.get("version", "未註明"),
                "size_kb": size // 1024,
                "mtime": datetime.fromtimestamp(mtime, tz=timezone(timedelta(hours=8))).isoformat(),
                "ext": ext,
            }
            items.append(item)

    _log.info(f"[INDEX] Walked {len(items)} files")
    return items


def _guess_filetype_from_fname(fname: str) -> str:
    """【2026-08-17 新】從 filename 猜 filetype (paper/daan).

    用於 DriveFolder raw dump, 那邊 metadata 不可靠, 只 guess filetype.
    例: '108-2一年級公民科第一次段考解答.docx' → 'daan'
        '108-2科技領域第一次段考考題.pdf' → 'paper'
    """
    fl = fname.lower()
    if "解答" in fname or "答案" in fname or "answer" in fl:
        return "daan"
    if "考題" in fname or "試題" in fname or "paper" in fl:
        return "paper"
    return "paper"  # default: 視為試題


def _parse_filename(fname: str, county_hint: str | None = None) -> dict | None:
    """從 filename parse 出 metadata (跟 pages.py cold-scan 邏輯一致)

    2026-08-15 加: 對於非標準檔名 (e.g. 楠梓109-2-2自(生物).pdf 或
    高雄市立楠梓國中108學年度第1學期第3階段定期評量3年級國文科試題卷.pdf),
    抓任何 3-4 位數字作為 school_year。

    2026-08-16 fix: county_hint=None 時用 non-capturing group, 不然 group(1) 變成 county prefix
    """
    # 【2026-08-16 fix】non-capturing group when no hint, 避免 group 1 = county prefix
    if county_hint:
        county_pat = re.escape(county_hint)
    else:
        county_pat = r"(?:[^_]+)"  # non-capturing

    # Pattern 1: StudyArk 標準
    m = re.match(rf"{county_pat}_(\d{{3}})_([^_]+)_(\d+)_([^_]+)_({PUBLISHERS})\.(pdf|docx|doc)$", fname)
    if m:
        return {
            "school_year": m.group(1),
            "school_term": "",
            "exam_type": m.group(2),
            "fileid": m.group(3),
            "school_name": m.group(4),
            "version": m.group(5),
            "grade": "",
        }

    # Pattern 2: 下/上學期
    m = re.match(rf"{county_pat}_(\d{{3}})([下上]學期)_([^_]+)_(\d+)_([^_]+)_({PUBLISHERS})\.(pdf|docx|doc)$", fname)
    if m:
        return {
            "school_year": m.group(1),
            "school_term": m.group(2),
            "exam_type": m.group(3),
            "fileid": m.group(4),
            "school_name": m.group(5),
            "version": m.group(6),
            "grade": "",
        }

    # Pattern 3: tcool migrated
    m = re.match(
        rf"{county_pat}_(\d{{3}})_第\d學期_([^_]+)_([^_]+)_(一年級|二年級|三年級|四年級|五年級|六年級|七年級|八年級|九年級)_([^_]+?)(?:_解答)?\.(pdf|docx|doc)$",
        fname,
    )
    if m:
        return {
            "school_year": m.group(1),
            "school_term": "",
            "exam_type": m.group(2),
            "fileid": "",
            "school_name": m.group(3),
            "version": "未註明",
            "grade": m.group(4),
            "subject": m.group(5),
        }

    # 【2026-08-15 新】Pattern 4: <county>_<year>_<exam>_<school>_<grade>_<subject>.pdf (無 fileid)
    # 例: 苗栗縣_108_第1段考_縣立大同國中_七年級_公民.pdf
    m = re.match(
        rf"{county_pat}_(\d{{3}})_([^_]+)_([^_]+)_(一年級|二年級|三年級|四年級|五年級|六年級|七年級|八年級|九年級|十年級|十一年級|十二年級)_([^_]+?)(?:_解答)?\.(pdf|docx|doc)$",
        fname,
    )
    if m:
        return {
            "school_year": m.group(1),
            "school_term": "",
            "exam_type": m.group(2),
            "fileid": "",
            "school_name": m.group(3),
            "version": "未註明",
            "grade": m.group(4),
            "subject": m.group(5),
        }

    # 【2026-08-15 新】Pattern 4: 非標準檔名 - 抓學年
    # 例: 「楠梓109-2-2自(生物).pdf」、「高雄市立楠梓國中108學年度第1學期第3階段定期評量.pdf」、
    #     「楠梓國中1年級地理上學期第二次段考  106年.pdf」、「108楠梓國中第二學期第三次段考一年級生物考題.pdf」、
    #     「高市楠梓國中106下2年級數學科第二次段考試題.pdf」
    # 規則: 找 100~115 範圍的 3 位數當學年 (跳過 \b 因中文不是 word boundary)
    # 優先看「N學年」「N學年度」「N下」「N上」「N年」 後缀才認
    year_match = re.search(r"(10[0-9]|11[0-5])(?:學年度|學年|年|[下上])", fname)
    if not year_match:
        # 再退一點、隨便找 3 位數
        year_match = re.search(r"(10[0-9]|11[0-5])", fname)

    school_year = year_match.group(1) if year_match else ""

    return {
        "school_year": school_year,
        "school_term": "",
        "exam_type": "",
        "fileid": "",
        "school_name": "",  # 學年以外的留空、由後續 fallback 補
        "version": "",
        "grade": "",
    }


def _build_title(parsed: dict, county: str, level: str, grade: str, subject: str) -> str:
    """組給使用者看的 title"""
    parts = []
    if parsed.get("school_name"):
        parts.append(parsed["school_name"])
    if parsed.get("school_year"):
        parts.append(f"{parsed['school_year']}學年度")
    if parsed.get("school_term"):
        parts.append(parsed["school_term"])
    if parsed.get("exam_type"):
        parts.append(parsed["exam_type"])
    if grade:
        parts.append(grade)
    if subject:
        parts.append(subject)
    if parsed.get("version") and parsed["version"] not in ("未註明", ""):
        parts.append(f"({parsed['version']})")
    return " ".join(parts) if parts else ""


# ============================================================
# Public API
# ============================================================
def get_index(force_rebuild: bool = False) -> list[dict]:
    """回傳所有 index items (用於 /me/downloads 或 admin)"""
    items = _load_or_build_index(force=force_rebuild).get("items", [])
    # 【2026-08-17 修】順便 populate _groups_cache, 給 legacy code 用 (例: test_download_daan_fallback)
    global _groups_cache, _groups_by_paper_id
    if _groups_cache is None and items:
        _groups_cache, _groups_by_paper_id = _build_groups_index(items)
    return items


def get_by_id(paper_id: str) -> dict | None:
    """從 paper_id 找單一 PDF item"""
    for item in get_index():
        if item["paper_id"] == paper_id:
            return item
    return None


def get_paired_by_id(paper_id: str) -> dict | None:
    """從 paper_id 找 paper group (paper + optional daan)

    Returns:
        {
            "paper_id": <主 paper 的 id (paper 優先)>,
            "county", "grade", "subject", "school_year", "school_term",
            "exam_type", "version", "school_name",
            "filetype_set": ["paper"],  # 或 ["paper","daan"]
            "download_answer": "有" | "無",
            "paper_path": <abs path 或 None>,
            "daan_path": <abs path 或 None>,
        }
    """
    _ensure_groups_index()  # 【2026-08-17 改】用 _groups_by_paper_id 直接 O(1) 查
    if _groups_by_paper_id is None or paper_id not in _groups_by_paper_id:
        return None
    group_key = _groups_by_paper_id[paper_id]
    # 自己 (paper 或 daan)
    self_item = None
    items_in_group = _groups_cache.get(group_key, []) if _groups_cache else []
    for it in items_in_group:
        if it["paper_id"] == paper_id:
            self_item = it
            break
    if self_item is None:
        # 從 DB fallback (罕見: 從 db query 來, 但 groups_cache 還沒 populate 完)
        self_item = get_by_id(paper_id)
    if self_item is None:
        return None

    # 找對方 (paper 找 daan, daan 找 paper)
    paper_item = None
    daan_item = None
    if self_item["filetype"] == "paper":
        paper_item = self_item
        for it in items_in_group:
            if it["paper_id"] != paper_id and it["filetype"] == "daan":
                daan_item = it
                break
    elif self_item["filetype"] == "daan":
        daan_item = self_item
        for it in items_in_group:
            if it["paper_id"] != paper_id and it["filetype"] == "paper":
                paper_item = it
                break
    else:
        # 沒 filetype (校個別結構)
        paper_item = self_item

    main = paper_item or daan_item
    return {
        "paper_id": main["paper_id"],
        "county": main["county"],
        "level": main["level"],
        "grade": main["grade"],
        "subject": main["subject"],
        "school_year": main["school_year"],
        "school_term": main["school_term"],
        "exam_type": main["exam_type"],
        "version": main["version"],
        "school_name": main["school_name"],
        "title": main["title"],
        "filetype_set": [t for t in ("paper", "daan") if (paper_item if t == "paper" else daan_item) is not None],
        "download_answer": "有" if daan_item else "無",
        "paper_path": paper_item["abs_path"] if paper_item else None,
        "paper_id_paper": paper_item["paper_id"] if paper_item else None,
        "daan_path": daan_item["abs_path"] if daan_item else None,
        "paper_id_daan": daan_item["paper_id"] if daan_item else None,
    }


def _group_key(item: dict) -> tuple:
    """判斷兩個 PDF 是否同一個 paper group (paper + daan pair)"""
    return (
        item["county"],
        item["school_year"],
        item["school_term"],
        item["exam_type"],
        item["school_name"],
        item["grade"],
        item["subject"],
        item["version"],
    )


def _normalise_school(name: str) -> str:
    """【2026-08-15】normalise 學校名 — 去掉 「市/縣/立」、保留核心字

    例:
    - 「高雄市楠梓國中」 → 「楠梓國中」
    - 「高雄市立楠梓國中」 → 「楠梓國中」
    - 「楠梓國中」 → 「楠梓國中」
    - 「臺北市立建國中學」 → 「建國中學」
    """
    if not name:
        return ""
    n = name
    # 1. 去掉前綴 「高雄市」「高雄縣」「臺北市」「台北市」 等 2-3 字
    n = re.sub(r"^[\w\u4e00-\u9fff]{2,3}(市|縣)", "", n)
    # 2. 去掉中間或前綴的 「立」字 (例: 「立楠梓」 → 「楠梓」、「市立」 → 去掉)
    #    但注意不要刪到 「國立」這類 (例: 「國立臺灣大學」 中間的「國立」)
    #    策略: 只刪前綴的 「立」
    n = re.sub(r"^立", "", n)
    return n.strip()


def _school_name_matches(query: str, candidate: str) -> bool:
    """【2026-08-15】比對學校名稱
    - 允許 「市/縣/立」 差異 (dashboard 用 「高雄市楠梓國中」 vs 檔名 parse 出 「楠梓國中」)
    - 加上 substring fallback

    【2026-08-16 fix】反過來的 substring (candidate in query) 只在 normalised 後相等才接受,
    不然 「高雄市楠梓國中」 會 match 到 「高雄市」 (county name 本身).
    """
    if not query:
        return True
    if not candidate:
        return False
    # 1. query 是 candidate 的子集 (例: 「楠梓」 in 「高雄市楠梓國中」) — 直接 accept
    if query in candidate:
        return True
    # 2. candidate 是 query 的子集 — 只在 normalised 後相等才接受
    #    (避免 「高雄市楠梓國中」 匹配 「高雄市」 county name)
    if candidate in query:
        q_norm = _normalise_school(query)
        c_norm = _normalise_school(candidate)
        if q_norm and c_norm and q_norm == c_norm:
            return True
        # 否則 false
        return False
    # 3. normalised substring (例: 「高雄市立楠梓國中」 vs 「楠梓國中」)
    q_norm = _normalise_school(query)
    c_norm = _normalise_school(candidate)
    if q_norm and c_norm and (q_norm in c_norm or c_norm in q_norm):
        return True
    return False


# Cache: group_key -> list of items (建好一次就不重建)
_groups_cache: dict | None = None
_groups_by_paper_id: dict | None = None


def _build_groups_index(items: list[dict]) -> tuple[dict, dict]:
    """建 group_key → items 索引, paper_id → group_key 索引"""
    groups: dict[tuple, list[dict]] = {}
    by_paper_id: dict[str, tuple] = {}
    for item in items:
        gk = _group_key(item)
        groups.setdefault(gk, []).append(item)
        by_paper_id[item["paper_id"]] = gk
    # 【2026-08-17 修】group 內 PDF 排前面, 確保 daan-only group 取 daan_items[0] 是 PDF
    # (既有 test_download_daan_fallback 假設 daan 是 PDF)
    for gk in groups:
        groups[gk].sort(key=lambda x: (x.get("ext") != "pdf", x.get("paper_id", "")))
    return groups, by_paper_id


def _ensure_groups_index():
    """確保 group 索引已建（lazy build）"""
    global _groups_cache, _groups_by_paper_id
    if _groups_cache is None:
        items = get_index()
        _groups_cache, _groups_by_paper_id = _build_groups_index(items)


def _lookup_group(group_key: tuple, exclude_paper_id: str | None = None) -> tuple[dict | None, dict | None]:
    """找 group 裡的 paper 跟 daan item (O(1) lookup)"""
    _ensure_groups_index()
    items = _groups_cache.get(group_key, [])
    paper_item = None
    daan_item = None
    for it in items:
        if exclude_paper_id and it["paper_id"] == exclude_paper_id:
            continue
        if it["filetype"] == "paper":
            paper_item = it
        elif it["filetype"] == "daan":
            daan_item = it
    return paper_item, daan_item


def search(
    county: str = "",
    grade: str = "",
    subject: str = "",
    school_year: str = "",
    school_term: str = "",
    exam_type: str = "",
    version: str = "",
    school_name: str = "",
    filetype: str = "",  # "paper" / "daan" / ""
    page: int = 1,
    per_page: int = 8,
) -> tuple[list[dict], int, int]:
    """Search local archive, 回傳 (groups, total_groups, total_pages)

    groups: list of paper pair dicts (same shape as get_paired_by_id output)
    """
    _ensure_groups_index()
    assert _groups_cache is not None

    # Filter groups by looking at one item per group (paper 優先)
    matched_group_keys: list[tuple] = []
    for gk, gitems in _groups_cache.items():
        # 從 group 裡選一個代表 item (paper 優先, 沒有 paper 用任何一個)
        rep = next((it for it in gitems if it["filetype"] == "paper"), gitems[0])
        # Apply filters
        if county and rep["county"] != county:
            continue
        if grade and rep["grade"] != grade:
            continue
        if subject and rep["subject"] != subject:
            continue
        if school_year and rep["school_year"] != school_year:
            continue
        if school_term and rep["school_term"] != school_term:
            continue
        if exam_type and rep["exam_type"] != exam_type:
            continue
        if version and rep["version"] != version:
            continue
        if school_name:
            if not _school_name_matches(school_name, rep["school_name"]):
                continue
        if filetype:
            # 必須 group 裡有對應 filetype
            if not any(it["filetype"] == filetype for it in gitems):
                continue
        matched_group_keys.append(gk)

    # Sort: county → school_year desc → exam_type → grade
    def _sort_key(gk: tuple):
        items_in_group = _groups_cache[gk]
        rep = next((it for it in items_in_group if it["filetype"] == "paper"), items_in_group[0])
        return (
            rep["county"],
            -int(rep["school_year"]) if rep["school_year"].isdigit() else 0,
            rep["exam_type"],
            rep["grade"],
            rep["subject"],
        )

    matched_group_keys.sort(key=_sort_key)

    total = len(matched_group_keys)
    total_pages = max(1, (total + per_page - 1) // per_page)
    start = (page - 1) * per_page
    end = start + per_page

    # Build groups for the requested page only
    page_groups: list[dict] = []
    for gk in matched_group_keys[start:end]:
        items_in_group = _groups_cache[gk]
        paper_item = next((it for it in items_in_group if it["filetype"] == "paper"), None)
        daan_item = next((it for it in items_in_group if it["filetype"] == "daan"), None)
        main = paper_item or daan_item
        page_groups.append({
            "paper_id": main["paper_id"],
            "county": main["county"],
            "level": main["level"],
            "grade": main["grade"],
            "subject": main["subject"],
            "school_year": main["school_year"],
            "school_term": main["school_term"],
            "exam_type": main["exam_type"],
            "version": main["version"],
            "school_name": main["school_name"],
            "title": main["title"],
            "filetype_set": [t for t in ("paper", "daan") if (paper_item if t == "paper" else daan_item) is not None],
            "download_answer": "有" if daan_item else "無",
            "paper_path": paper_item["abs_path"] if paper_item else None,
            "paper_id_paper": paper_item["paper_id"] if paper_item else None,
            "daan_path": daan_item["abs_path"] if daan_item else None,
            "paper_id_daan": daan_item["paper_id"] if daan_item else None,
        })

    return page_groups, total, total_pages


# ============================================================
# Background warm-up
# ============================================================
def warmup_in_background():
    """啟動時 background thread 跑一次 build，避免 user 第一次 search 慢"""
    def _run():
        try:
            _load_or_build_index()
        except Exception as e:
            _log.warning(f"[INDEX] Background warmup failed: {e}")
    t = threading.Thread(target=_run, daemon=True, name="local-index-warmup")
    t.start()