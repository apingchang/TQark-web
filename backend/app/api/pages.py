"""
HTML page routes

- GET /         → landing page
- GET /dashboard → user dashboard
- GET /admin    → admin panel
- GET /static/*  → 靜態檔案
"""

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.deps import get_current_user_from_token, require_admin, require_login
from app.db.models import AccessRequest, AuditLog, User
from app.db.session import get_db

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