"""
HTML page routes

- GET /         → landing page
- GET /dashboard → user dashboard
- GET /admin    → admin panel
- GET /static/*  → 靜態檔案
"""

from pathlib import Path

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
    - 中央: login / status / access 申請
    - 右側: 平台資訊
    - 下方 banner: AdSense placeholder

    同時拿 stats (paper 總數、approved user 數) 顯示在右側。
    """
    from sqlalchemy import func

    # 拿 platform stats
    stats: dict = {}
    try:
        from app.db.models import DownloadHistory, User as UserModel

        # 總 paper 數 (從下載紀錄去重 fileid+filetype)
        # 簡化: 總下載次數
        paper_count = (await db.execute(
            select(func.count(DownloadHistory.id)).where(DownloadHistory.filetype == "paper")
        )).scalar() or 0
        stats["total_papers"] = paper_count

        # approved user 數
        approved_count = (await db.execute(
            select(func.count(UserModel.id)).where(UserModel.permission <= 7, UserModel.role != "admin")
        )).scalar() or 0
        stats["approved_users"] = approved_count
    except Exception:
        stats = {"total_papers": 0, "approved_users": 0}

    return templates.TemplateResponse(
        "landing.html",
        {**_common_ctx(user), "request": request, "stats": stats},
    )


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    user: User = Depends(require_login),
):
    return templates.TemplateResponse(
        "dashboard.html",
        {
            **_common_ctx(user),
            "request": request,
            "user_full": user,  # 完整 User object 給 template 用 datetime 等
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