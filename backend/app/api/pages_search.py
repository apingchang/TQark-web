"""
Search page routes (2026-08-17 新 Pages Split 3).

Provides routes for 考題搜尋 (search) + 下載:
- /ui/search (POST/GET)        → 搜尋結果頁 (render search_results.html)
- /ui/download/paper/{paper_id} (GET) → 下載單一 PDF (從 DB lookup)
- /ui/download/{classid}/{fileid} (GET) → 舊 StudyArk download (向後相容)
- /api/available-schools (GET) → cascading dropdown: county → 學校清單
- /api/available-filters (GET) → cascading dropdown: county+school → filter values

內含 helper functions:
- _render_search_results: 主搜尋結果 render
- _scan_schools_from_disk / _save_snapshot / _load_snapshot / _refresh_snapshot_async / _get_cached_schools:
  schools cache (避免每次請求 walk NAS)
- api_available_schools / api_available_filters: dropdown API

【拆分原則】
- 搜尋相關 routes + helpers 都集中這裡
- pages.py 留 dashboard / landing / admin / me/downloads / tools
- 共用 helpers 從 pages_helpers import
- 共用 consts (CAP_DIR, CEEC_DIR, _scan_pdf_tree) 從 pages_cap_ceec import
"""
import json as _json
import os
import time as _time
import threading as _threading
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.pages_helpers import (
    templates,
    _user_ctx,
    _common_ctx,
    _get_build_mtime,
)
from app.api.pages_cap_ceec import (
    CAP_DIR,
    CEEC_DIR,
    _scan_pdf_tree,
    _render_cap_exam_results,    # alias 給 ui_search_post/get
    _render_ceec_exam_results,
)
from app.core.db_helpers import hash_ip as _hash_ip, log_action
from app.core.deps import require_approved, require_login
from app.db.models import AuditLog, DownloadHistory, User
from app.db.session import get_db
from app.scraper import studyark
from app.scraper.db import get_available_filters

search_router = APIRouter(tags=["search"])

# 【2026-08-17 Pages Split 3】schools cache 全域變數 (從 pages.py 搬過來)
_disk_schools_cache: dict = {"data": {}, "ts": 0.0}
_SCHOOLS_SNAPSHOT_PATH = __import__('pathlib').Path(__file__).resolve().parent.parent.parent / "state" / "schools_snapshot.json"
_SCHOOLS_CACHE_TTL = 60  # 1 minute (背景 archive 持續寫新檔, cache 1 min 讓 user 看到新內容)
_SNAPSHOT_TTL_SECONDS = 30 * 60  # 30 min

# ──────────────────────────────────────────────────────────────
# Segment 1: _render_search_results (主搜尋 render)
# ──────────────────────────────────────────────────────────────

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
    """【2026-08-15 改】搜尋改走 local folder index (不再 call StudyArk 網頁)"""
    from app.core.db_helpers import log_action
    from app.scraper import local_index
    from app.scraper import db as db_mod

    error = None
    results: list[dict] = []
    total = 0
    total_page = 1

    # PAPERS_PER_PAGE 沿用舊值 (8 groups × 2 files = 16 files/頁)
    PAPERS_PER_PAGE = 8

    # 【2026-08-15 改】直接從 local index 找
    fallback_info = {"fallback_unclassified": False, "fallback_count": 0, "fallback_filters_dropped": []}
    try:
        results, total, total_page, fallback_info = db_mod.search_files_grouped(
            county=county,
            grade=grade,
            subject=subject,
            school_year=school_year,
            school_term=school_term,
            exam_type=exam_type,
            version=version,
            school_name=school_name,
            filetype="",  # 不限制 filetype (pair 顯示)
            page=page,
            per_page=PAPERS_PER_PAGE,
        )
    except Exception as e:
        error = f"Local index 搜尋失敗: {e}"
        _log.exception("Local search failed")

    filter_info = f"county={county or 'all'},school={school_name or 'all'},grade={grade or 'all'},subject={subject or 'all'}"

    # Audit log
    await log_action(
        db,
        action="search",
        user_id=user.id,
        target=f"grade={grade},subject={subject}",
        detail=f"source=local;page={page};{filter_info};total={total}",
    )
    await db.commit()

    # 顯示用 dict (template 用來生成個別移除連結)
    params_display = {
        k: v for k, v in [
            ("county", county),
            ("grade", grade),
            ("subject", subject),
            ("school_year", school_year),
            ("school_term", school_term),
            ("exam_type", exam_type),
            ("version", version),
            ("daan", daan),
            ("school_name", school_name),
        ] if v
    }
    # 顯示用字串 (audit log)
    search_params_str = (
        ", ".join(f"{k}={v}" for k, v in params_display.items())
        or "(無條件)"
    )

    # 給 template 用:根據當前 page 組 querystring(給「下一頁」連結用)
    def qs_for_page(p: int) -> str:
        from urllib.parse import urlencode
        base = {
            "county": county,
            "grade": grade,
            "subject": subject,
            "school_year": school_year,
            "school_term": school_term,
            "exam_type": exam_type,
            "version": version,
            "school_name": school_name,
            "page": p,
        }
        base = {k: v for k, v in base.items() if v}
        return urlencode(base)

    return templates.TemplateResponse(
        "search_results.html",
        {
            **_common_ctx(user),
            "request": request,
            "results": results,
            "error": error,
            "search_params": search_params_str,  # audit log 字串
            "params_display": params_display,  # template chip 移除連結用
            # 【2026-08-17 新】cache buster (file mtime 自動 invalidate)
            "deploy_ts": int(os.path.getmtime(Path(__file__).parent.parent / "static" / "cache_check.js")),
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
            # 【2026-08-17 新】DriveFolder fallback info (template 顯示「含未分類檔案」)
            "fallback_unclassified": fallback_info["fallback_unclassified"],
            "fallback_count": fallback_info["fallback_count"],
            "fallback_filters_dropped": fallback_info["fallback_filters_dropped"],
            # 【2026-08-17 新】本頁實際檔案數 (paper + daan row 個別算), 給 template 顯示
            "files_in_page": sum(
                1 + (1 if (g.get("paper_id_daan") and g.get("daan_path")) else 0)
                for g in results
            ),
            "files_in_page_daan": sum(
                1 for g in results if g.get("paper_id_daan") and g.get("daan_path")
            ),
        },
    )



# === 【2026-07-31 新】三好工具 (Tools) ===
# - 只允許 perm <= 1 (家人 + 管理員)
# - 兩個工具: 日文翻譯 / 信用卡帳單
# - 現在是 placeholder, 之後實作


# ──────────────────────────────────────────────────────────────
# Segment 2: ui_search_post, ui_search_get, ui_download_paper, ui_download
# ──────────────────────────────────────────────────────────────

@search_router.post("/ui/search", response_class=HTMLResponse)

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


@search_router.get("/ui/search", response_class=HTMLResponse)
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


@search_router.get("/ui/download/paper/{paper_id}")
async def ui_download_paper(
    paper_id: str,
    request: Request,
    filetype: str = Query("paper", regex="^(paper|daan)$"),
    user: User = Depends(require_approved),
    db: AsyncSession = Depends(get_db),
):
    """【2026-08-15 新】下載 local archive 內的 PDF（搜尋改 local 後用這個 endpoint）。

    paper_id = SHA1(canonical_relative_path)[:16]
    filetype = paper / daan（如果指定的不存在、但伴侶存在，會 fallback 到伴侶）

    【路由順序】這個 route 必須在 /ui/download/{classid}/{fileid} 之前註冊，
    否則會被舊的 route 搶走（classid=paper, fileid={paper_id}）。
    """
    from urllib.parse import quote

    from app.core.db_helpers import hash_ip, log_action
    from app.db.models import DownloadHistory
    from app.scraper import local_index
    from app.scraper import db as db_mod
    import logging
    _dl_logger = logging.getLogger("tqark.local_download")

    group = local_index.get_paired_by_id(paper_id)
    if group is None:
        raise HTTPException(404, f"找不到 paper_id={paper_id}")

    # 選 paper / daan 對應 path
    # 【2026-08-15 fix】只單獨存在 paper 或 daan 時 → fallback 到對方
    if filetype == "daan":
        chosen_path = group["daan_path"] or group["paper_path"]
        chosen_id = group["paper_id_daan"] or group["paper_id_paper"] or paper_id
        actual_filetype = "daan" if group["daan_path"] else "paper"  # 記下實際提供哪個
    else:
        chosen_path = group["paper_path"] or group["daan_path"]
        chosen_id = group["paper_id_paper"] or group["paper_id_daan"] or paper_id
        actual_filetype = "paper" if group["paper_path"] else "daan"  # 記下實際提供哪個

    if not chosen_path:
        raise HTTPException(404, f"找不到 paper_id={paper_id} 的 {filetype} 檔案")

    p = Path(chosen_path)
    if not p.exists():
        raise HTTPException(404, f"檔案不存在: {chosen_path}")

    try:
        pdf_bytes = p.read_bytes()
    except OSError as e:
        raise HTTPException(500, f"讀檔失敗: {e}")

    # 組下載檔名（顯示用）
    title_parts = []
    if group["school_name"]:
        title_parts.append(group["school_name"])
    if group["school_year"]:
        title_parts.append(f"{group['school_year']}學年度")
    if group["school_term"]:
        title_parts.append(group["school_term"])
    if group["exam_type"]:
        title_parts.append(group["exam_type"])
    if group["grade"]:
        title_parts.append(group["grade"])
    if group["subject"]:
        title_parts.append(group["subject"])
    if group["version"] and group["version"] not in ("未註明", ""):
        title_parts.append(f"({group['version']})")
    title_parts.append("答案" if actual_filetype == "daan" else "試題")
    download_filename = " ".join(title_parts) + ".pdf" if title_parts else p.name

    # DownloadHistory 記錄 (paper_id 為主，classid/fileid 留空)
    db_record = DownloadHistory(
        user_id=user.id,
        classid="LOCAL",
        fileid=chosen_id,
        filetype=actual_filetype,
        title=group["title"] or None,
        school_name=group["school_name"] or None,
        grade=group["grade"] or None,
        school_year=group["school_year"] or None,
        school_term=group["school_term"] or None,
        category=None,
        subject=group["subject"] or None,
        exam_type=group["exam_type"] or None,
        version=group["version"] or None,
        download_filename=download_filename,
        ip_hash=hash_ip(request.client.host if request.client else None),
        user_agent=request.headers.get("user-agent", "")[:512] or None,
    )
    db.add(db_record)
    await log_action(
        db,
        action="download_local",
        user_id=user.id,
        target=f"paper_id={paper_id}",
        detail=f"filetype={filetype};path={chosen_path}",
        ip=str(request.client.host) if request.client else None,
    )
    await db.commit()

    safe_filename = download_filename.encode("ascii", errors="ignore").decode("ascii") or "exam.pdf"
    encoded_filename = quote(download_filename)
    headers = {
        "Content-Disposition": (
            f"attachment; "
            f'filename="{safe_filename}"; '
            f"filename*=UTF-8''{encoded_filename}"
        ),
        "X-Download-Filename": encoded_filename,
    }

    _dl_logger.info(f"[LOCAL DL] paper_id={paper_id} filetype={filetype} -> {chosen_path} ({len(pdf_bytes)} bytes)")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers=headers,
    )


@search_router.get("/ui/download/{classid}/{fileid}")
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

# ──────────────────────────────────────────────────────────────
# Segment 3: schools scan helpers + cascading dropdown APIs
# ──────────────────────────────────────────────────────────────

def _scan_schools_from_disk() -> dict:
    """從 DB 讀學校 dropdown (2026-08-17 改, 之前是 walk NAS).

    Returns dict[county_name] = list of {name, file_count, path}.

    【效能改善】原本 walk NAS 60-90 秒 → 從 DB 讀 <100ms.
    """
    from app.scraper import db
    result = {}
    try:
        conn = db._connect()
        rows = conn.execute("""
            SELECT county, school_name, COUNT(*) AS fc
            FROM files
            WHERE school_name != '' AND school_name != county
            GROUP BY county, school_name
            ORDER BY county, fc DESC, school_name
        """).fetchall()
        for r in rows:
            county, school, fc = r["county"], r["school_name"], r["fc"]
            result.setdefault(county, []).append({
                "name": school,
                "file_count": fc,
                "path": f"{county}/{school}/",
            })
    except Exception:
        pass
    return result



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


@search_router.get("/api/available-schools", response_class=JSONResponse)
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


@search_router.get("/api/available-filters", response_class=JSONResponse)
async def api_available_filters(
    county: str = Query("", description="Filter by county"),
    school_name: str = Query("", description="Filter by school (use token matching)"),
    user: User = Depends(require_login),
):
    """【2026-08-17 新】cascading dropdown 後段: 給 county + school 回該範圍所有 filter values.

    Used by dashboard cascading dropdown (school → year/grade/subject/exam/term/version).
    Returns:
        {
            "school_year": ["114", "113", ...],  # DESC
            "grade": [...], "subject": [...], "school_term": [...],
            "exam_type": [...], "version": [...]
        }
    """
    from app.scraper.db import get_available_filters
    filters = get_available_filters(county=county, school_name=school_name)
    return JSONResponse(filters)

