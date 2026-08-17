"""
Helpers for HTML page routes (2026-08-17 新 Pages Split 1).

Contains utility functions used by pages.py routes:
- _background_fetch_companion (parallel download helper)
- _user_ctx / _common_ctx (Jinja template context builders)
- _get_build_mtime (for footer display)
- _permission_name (for admin panel)

Pure helpers, no FastAPI routes. Imported by pages.py.

【拆分原則】
- 不放 routes (那屬 pages.py 或其他 route modules)
- 不放 stateful globals (router 仍留在 pages.py)
- 每個 helper 是 pure function (除了 _background_fetch_companion 用 threading)
"""
import os
import threading
import time as _time
from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.config import settings
from app.db.models import User

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# 【2026-08-17 新】Jinja2Templates instance (共用的 TemplateResponse 物件)
# pages.py 跟 pages_cap_ceec.py 都從這 import 避免重複宣告
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# PERMISSION_NAMES (admin panel 用)
PERMISSION_NAMES = {
    0: "訪客 (Guest)",
    1: "成員 (Member)",
    2: "管理員 (Admin)",
    7: "Family",
    8: "已註冊",
    9: "待審核",
    99: "訪客 (未登入)",
}

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
        "build_mtime": _get_build_mtime(),
    }


# 【2026-08-15 新】取得所有相關原始檔最新的 mtime, 讓側邊欄顯示「程式最後修改時間」
_BUILD_MTIME_CACHE: tuple[float, str] | None = None
_BUILD_MTIME_PATTERNS = (
    # (label, glob pattern under backend/)
    ("backend", "**/*.py"),
    ("templates", "**/*.html"),
    ("static", "**/*.js"),
)


def _get_build_mtime() -> str:
    """回傳 backend 所有 .py / .html / .js 的最新 mtime (ISO 格式)。"""
    global _BUILD_MTIME_CACHE
    import os as _os
    import time as _time
    now = _time.time()
    if _BUILD_MTIME_CACHE and (now - _BUILD_MTIME_CACHE[0]) < 30:
        return _BUILD_MTIME_CACHE[1]

    latest = 0.0
    backend_dir = Path(__file__).resolve().parent.parent  # backend/
    for label, pattern in _BUILD_MTIME_PATTERNS:
        for path in backend_dir.glob(pattern):
            try:
                m = path.stat().st_mtime
                if m > latest:
                    latest = m
            except OSError:
                pass

    if latest == 0.0:
        result = "未知"
    else:
        from datetime import datetime, timezone, timedelta as _td
        dt = datetime.fromtimestamp(latest, tz=timezone(_td(hours=8)))
        result = dt.strftime("%Y-%m-%d %H:%M:%S")
    _BUILD_MTIME_CACHE = (now, result)
    return result


def _permission_name(perm: int) -> str:
    """Return human-readable permission name."""
    return PERMISSION_NAMES.get(perm, f"未知 ({perm})")

