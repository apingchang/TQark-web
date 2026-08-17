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

# 【2026-08-17 新】強制 no-cache HTML responses (避免 internet proxy / browser cache 住舊 JS)
# dashboard_form.js 用 ?v=mtime 還是會被某些 proxy strip 或 cache hit
from starlette.middleware.base import BaseHTTPMiddleware

class NoCacheHTMLMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        ct = response.headers.get("content-type", "")
        # 只針對 HTML responses, 不影響 API / static files
        if "text/html" in ct:
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

app.add_middleware(NoCacheHTMLMiddleware)

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
    """啟動時建 DB table + 預先 warmup schools / archive_counts / pdf_tree cache"""
    await init_db()

    import threading
    import time as _time

    def _warmup_schools():
        # 【2026-07-31 新】schools scan (避免 user 第一次 dropdown 慢 18s)
        from app.api.pages import _scan_schools_from_disk, _save_snapshot, _disk_schools_cache
        try:
            data = _scan_schools_from_disk()
            _save_snapshot(data)
            _disk_schools_cache["data"] = data
            _disk_schools_cache["ts"] = _time.time()
            print(f"[startup] schools cache warmed: {sum(len(v) for v in data.values())} schools, {sum(s['file_count'] for v in data.values() for s in v)} files", flush=True)
        except Exception as e:
            print(f"[startup] schools warmup failed: {e}", flush=True)

    def _warmup_archive_counts():
        # 【2026-08-07 改】archive_counts scan 改 lazy + background, 不再 startup 同步跑 174s
        # 觸發 background scan 讓它在 startup 後跑, user 訪問時可能已 populated
        from app.api.pages import _get_cached_archive_counts
        try:
            data = _get_cached_archive_counts()  # 觸發 bg scan, return placeholder
            print(f"[startup] archive_counts bg scan started (預計 ~3 分鐘; user 訪問時會 hit placeholder 或 cache)", flush=True)
        except Exception as e:
            print(f"[startup] archive_counts warmup failed: {e}", flush=True)

    def _warmup_pdf_tree():
        # 【2026-08-03 新】CAP + CEEC PDF tree scan (避免 dashboard 第一次 render 慢 11s)
        # 呼叫 _scan_pdf_tree() 會自動 populate in-memory cache
        from app.api.pages import _scan_pdf_tree, CAP_DIR, CEEC_DIR
        try:
            t0 = _time.time()
            cap_items = _scan_pdf_tree(CAP_DIR)
            ceec_items = _scan_pdf_tree(CEEC_DIR)
            print(f"[startup] pdf_tree cache warmed: CAP={len(cap_items)} files, CEEC={len(ceec_items)} files ({_time.time() - t0:.1f}s)", flush=True)
        except Exception as e:
            print(f"[startup] pdf_tree warmup failed: {e}", flush=True)

    # Schools cache warmup (existing)
    threading.Thread(target=_warmup_schools, daemon=True, name="warmup-schools").start()
    # 【2026-08-03 新】archive counts + pdf tree warmup — 解決 dashboard cold cache 慢
    threading.Thread(target=_warmup_archive_counts, daemon=True, name="warmup-archive-counts").start()
    threading.Thread(target=_warmup_pdf_tree, daemon=True, name="warmup-pdf-tree").start()

    def _warmup_local_index():
        # 【2026-08-15 新】local archive index (考題搜尋走本地,避免第一次 search 慢)
        from app.scraper import local_index
        try:
            t0 = _time.time()
            items = local_index.get_index()  # 會 trigger build_or_load
            print(f"[startup] local_index cache warmed: {len(items)} items ({_time.time() - t0:.1f}s)", flush=True)
        except Exception as e:
            print(f"[startup] local_index warmup failed: {e}", flush=True)

    threading.Thread(target=_warmup_local_index, daemon=True, name="warmup-local-index").start()

    # 【2026-08-17 新】SQLite DB warmup + background full scan
    # 為什麼: dashboard dropdown 跟 search 都用 DB
    # - 啟動時先讀現有 DB (即使 DB 是空, dropdown 也能從 DB 顯示空 list)
    # - 背景跑一次 full scan 更新 DB (讓 archive 後新增的檔立即生效)
    def _warmup_search_db():
        from app.scraper import db as db_mod
        from pathlib import Path as _Path
        try:
            t0 = _time.time()
            db_mod.init_db()
            conn = db_mod._connect()
            count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            print(f"[startup] search_db: {count} items in DB ({_time.time() - t0:.2f}s)", flush=True)
        except Exception as e:
            print(f"[startup] search_db init failed: {e}", flush=True)
            return  # DB init fail, skip bg scan

        # Background full scan (不阻擋 startup)
        def _bg_scan():
            try:
                from app.scraper.local_index import _walk_archive
                t0 = _time.time()
                items = _walk_archive()  # uses ARCHIVE_ROOT constant
                db_mod.rebuild_from_items(items)
                print(f"[startup-bg] search_db full scan done: {len(items)} items ({_time.time() - t0:.1f}s)", flush=True)
            except Exception as e:
                print(f"[startup-bg] search_db full scan failed: {e}", flush=True)

        threading.Thread(target=_bg_scan, daemon=True, name="search-db-bg-scan").start()

    threading.Thread(target=_warmup_search_db, daemon=True, name="warmup-search-db").start()


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