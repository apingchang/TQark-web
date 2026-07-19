"""
TQark-web 入口 — FastAPI app

Phase 1.1: Google OAuth + JWT cookie + landing page
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import Cookie, FastAPI  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pathlib import Path  # noqa: E402

from app.api.auth import SESSION_COOKIE, router as auth_router  # noqa: E402
from app.api.pages import router as pages_router  # noqa: E402
from app.api.admin import router as admin_router  # noqa: E402
from app.api.access_requests import router as access_requests_router  # noqa: E402
from app.api.scraper import router as scraper_router  # noqa: E402
from app.config import settings  # noqa: E402
from app.core.security import decode_access_token  # noqa: E402
from app.db.session import init_db  # noqa: E402

app = FastAPI(
    title="TQark-web",
    description="Private invite-only web app for sharing StudyArk exam PDFs",
    version="0.1.2",
)

# /static mount(serve Bootstrap CSS / JS / favicon)
_static_dir = Path(__file__).resolve().parent / "static"
_static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

app.include_router(auth_router)
app.include_router(pages_router)
app.include_router(admin_router)
app.include_router(access_requests_router)
app.include_router(scraper_router)


@app.on_event("startup")
async def startup_event():
    """啟動時建 DB table"""
    await init_db()


@app.get("/health")
async def health() -> dict:
    """健全檢查"""
    return {
        "status": "ok",
        "env": settings.env,
        "version": "0.1.2",
    }


# / 路由在 pages.py(回傳 HTML)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=settings.port,
        reload=not settings.is_production,
    )