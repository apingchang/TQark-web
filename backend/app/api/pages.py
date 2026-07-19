"""
HTML page routes

- GET /         → landing page
- GET /dashboard → user dashboard
- GET /admin    → admin panel
- GET /static/*  → 靜態檔案
"""

from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, FileResponse
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
@router.post("/ui/search", response_class=HTMLResponse)
async def ui_search(
    request: Request,
    grade: str = Form(""),
    subject: str = Form(""),
    school_year: str = Form(""),
    school_term: str = Form(""),
    exam_type: str = Form(""),
    version: str = Form(""),
    daan: str = Form(""),
    user: User = Depends(require_approved),
    db: AsyncSession = Depends(get_db),
):
    """
    Form submit 觸發,跑 StudyArk search,顯示結果頁。
    """
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
    }
    # 去掉 None
    search_params = {k: v for k, v in search_params.items() if v}

    error = None
    results = []
    try:
        raw = await studyark.search_papers(**search_params)
        # StudyArk 通常回傳 { list: [...], total: N } 或直接 [...]
        if isinstance(raw, dict):
            results = raw.get("list") or raw.get("data") or raw.get("results") or []
            if not results and "items" in raw:
                results = raw["items"]
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
    )
    await db.commit()

    # 顯示用字串
    params_display = ", ".join(f"{k}={v}" for k, v in search_params.items()) or "(無條件)"

    return templates.TemplateResponse(
        "search_results.html",
        {
            **_common_ctx(user),
            "request": request,
            "results": results,
            "error": error,
            "search_params": params_display,
        },
    )


@router.get("/ui/download/{classid}/{fileid}")
async def ui_download(
    classid: str,
    fileid: str,
    filetype: str = Query("paper"),
    user: User = Depends(require_approved),
    db: AsyncSession = Depends(get_db),
):
    """下載 PDF(會 cache)"""
    from app.core.db_helpers import log_action

    try:
        fpath = await studyark.download_to_cache(classid, fileid, filetype)
    except FileNotFoundError as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        raise HTTPException(502, f"下載失敗: {e}")

    await log_action(
        db,
        action="download",
        user_id=user.id,
        target=f"{classid}/{fileid}",
        detail=f"filetype={filetype}",
    )
    await db.commit()

    return FileResponse(
        path=str(fpath),
        media_type="application/pdf",
        filename=f"{classid}_{fileid}_{filetype}.pdf",
    )