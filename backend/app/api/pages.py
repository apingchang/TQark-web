"""
HTML page routes

- GET /         → landing page
- GET /dashboard → user dashboard
- GET /admin    → admin panel
- GET /static/*  → 靜態檔案
"""

from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.deps import get_current_user_from_token, require_admin, require_approved, require_login
from app.db.models import AccessRequest, AuditLog, User
from app.db.session import get_db
from app.scraper import studyark

router = APIRouter(tags=["pages"])

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _user_ctx(user: User | None) -> dict:
    """把 User object 轉成 template 用的 dict"""
    if user is None:
        return {}
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "picture": user.picture,
        "role": user.role,
        "status": user.status,
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
):
    return templates.TemplateResponse(
        "landing.html",
        {**_common_ctx(user), "request": request},
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
        },
    )


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
):
    from app.core.db_helpers import log_action
    from app.db.models import utcnow

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

    # Audit log
    await log_action(
        db,
        action="search",
        user_id=user.id,
        target=f"grade={grade},subject={subject}",
        detail=f"page={page}",
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
            "page": page,
            "total": total,
            "total_page": total_page,
            "qs_for_page": qs_for_page,
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
    user: User = Depends(require_approved),
    db: AsyncSession = Depends(get_db),
):
    """Form submit(dashboard 的 search form)"""
    return await _render_search_results(
        request, user, db,
        grade=grade, subject=subject, school_year=school_year, school_term=school_term,
        exam_type=exam_type, version=version, daan=daan, page=page,
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
    user: User = Depends(require_approved),
    db: AsyncSession = Depends(get_db),
):
    """Page 切換(分頁按鈕 → querystring)"""
    return await _render_search_results(
        request, user, db,
        grade=grade, subject=subject, school_year=school_year, school_term=school_term,
        exam_type=exam_type, version=version, daan=daan, page=page,
    )


@router.get("/ui/download/{classid}/{fileid}")
async def ui_download(
    classid: str,
    fileid: str,
    request: Request,
    filetype: str = Query("paper", regex="^(paper|answer)$"),
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

    try:
        pdf_bytes, content_type = await studyark.download_pdf_stream(classid, fileid, filetype)
    except FileNotFoundError as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        raise HTTPException(502, f"下載失敗: {e}")

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