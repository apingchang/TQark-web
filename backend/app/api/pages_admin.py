"""
Admin + Tools page routes (2026-08-17 新 Pages Split 4).

Provides routes for 管理員面板 + 三好工具:
- /admin (GET)                       → 管理員面板 (audit log + access requests)
- /tools (GET)                       → 三好工具 index
- /tools/jp-translate (GET)          → 日文翻譯工具
- /tools/credit-card (GET)           → 信用卡帳單工具
- /api/tools/jp-upload (POST)        → 上傳圖片給日文翻譯
- /api/tools/jp-download/{task_id}/{engine} (GET) → 下載翻譯結果
- /api/tools/jp-status (GET)         → 查詢翻譯 status
- /api/tools/jp-reset (POST)          → 重置翻譯 status

Permission: 限 perm <= 1 (家人 + 管理員) 才能用 Tools, admin_panel 限 perm 0 (admin)

【拆分原則】
- Admin + Tools 都是限 perm 的功能, 不混進 search/dashboard
- pages.py 留 landing / dashboard / school_sources / batch_download_archive / me_downloads
- 共用 templates / helpers 從 pages_helpers import
- JP translator 從 app.tools.jp_translator import (已在 pages.py import 過)
"""
from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.pages_helpers import (
    templates,
    _user_ctx,
    _common_ctx,
)
from app.core.db_helpers import log_action
from app.core.deps import require_admin, require_approved, require_login
from app.db.models import AccessRequest, AuditLog, DownloadHistory, User
from app.db.session import get_db

admin_router = APIRouter(tags=["admin_tools"])

# ════════════════════════════════════════════════════════════════════
# Segment 1: admin_panel (管理員面板)
# ════════════════════════════════════════════════════════════════════

@admin_router.get("/admin", response_class=HTMLResponse)
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



from pydantic import BaseModel as _BaseModel
from app.core.db_helpers import hash_ip as _hash_ip

class _ArchiveBatchItem(_BaseModel):
    """CAP/CEEC archive batch item"""
    source: str  # "CAP" or "CEEC"
    rel: str  # relative path under CAP_DIR / CEEC_DIR
    subject: str | None = None
    grade: str | None = None
    school_year: str | None = None
    title: str | None = None


class _ArchiveBatchRequest(_BaseModel):
    items: list[_ArchiveBatchItem]



# ════════════════════════════════════════════════════════════════════
# Segment 2: tools (三好工具 + 日文翻譯 + 信用卡帳單)
# ════════════════════════════════════════════════════════════════════

@admin_router.get("/tools", response_class=HTMLResponse)
async def tools_index(
    request: Request,
    user: User = Depends(require_approved),
):
    """三好工具首頁 (限 perm 0 or 1)"""
    if not user or user.permission is None or user.permission > 1:
        raise HTTPException(
            status_code=403,
            detail="🔒 三好工具僅限家人/管理員使用 (需權限 0 或 1)",
        )
    return templates.TemplateResponse(
        "tools.html",
        {**_common_ctx(user), "request": request},
    )


@admin_router.get("/tools/jp-translate", response_class=HTMLResponse)
async def tools_jp_translate(
    request: Request,
    user: User = Depends(require_approved),
):
    """日文翻譯工具 (限 perm 0 or 1)"""
    if not user or user.permission is None or user.permission > 1:
        raise HTTPException(
            status_code=403,
            detail="🔒 此功能僅限家人/管理員使用 (需權限 0 或 1)",
        )
    return templates.TemplateResponse(
        "tools_jp_translate.html",
        {**_common_ctx(user), "request": request},
    )


@admin_router.get("/tools/credit-card", response_class=HTMLResponse)
async def tools_credit_card(
    request: Request,
    user: User = Depends(require_approved),
):
    """信用卡帳單工具 (限 perm 0 or 1)"""
    if not user or user.permission is None or user.permission > 1:
        raise HTTPException(
            status_code=403,
            detail="🔒 此功能僅限家人/管理員使用 (需權限 0 或 1)",
        )
    return templates.TemplateResponse(
        "tools_credit_card.html",
        {**_common_ctx(user), "request": request},
    )




# === 【2026-07-31 新】日文翻譯 API ===
# - POST /api/tools/jp-upload               (multipart: file + engine → 背景)
# - GET  /api/tools/jp-download?filename=... (下載翻譯結果, browser Save As)
# - GET  /api/tools/jp-status                (查 status, 前端 1.5s poll)
# - POST /api/tools/jp-reset                 (重置)

@admin_router.post("/api/tools/jp-upload")
async def jp_upload(
    request: Request,
    user: User = Depends(require_approved),
):
    """【2026-08-01 新】接收上傳 .docx + 啟動翻譯 (multipart, 限 perm 0/1)
    
    Form fields:
        file: .docx
        engine: google | minimax | all
    
    Returns:
        {ok, task_id, filename, engine, staging_path}
    """
    if not user or user.permission is None or user.permission > 1:
        raise HTTPException(403, "🔒 此 API 僅限家人/管理員使用")
    
    from fastapi import UploadFile, File, Form
    from app.tools.jp_translator import save_uploaded_file, start_translation
    
    form = await request.form()
    file = form.get("file")
    engine = form.get("engine", "all")
    
    if not file or not hasattr(file, "filename"):
        raise HTTPException(400, "file 必填 (multipart/form-data)")
    
    filename = file.filename
    if not filename or not filename.endswith(".docx"):
        raise HTTPException(400, f"只接受 .docx, 收到: {filename}")
    
    if engine not in ("google", "minimax", "all"):
        raise HTTPException(400, f"engine 必須是 google / minimax / all, 收到: {engine}")
    
    # Read + save to staging
    contents = await file.read()
    if len(contents) == 0:
        raise HTTPException(400, "file 是空的")
    if len(contents) > 50 * 1024 * 1024:  # 50MB limit
        raise HTTPException(400, f"file 太大 ({len(contents)/1024/1024:.1f}MB, 上限 50MB)")
    
    staging_path = save_uploaded_file(contents, filename)
    
    # Start translation (background)
    result = start_translation(str(staging_path), filename, engine)
    if not result.get("ok"):
        # Cleanup staging
        try:
            staging_path.unlink()
        except Exception:
            pass
        raise HTTPException(409, result.get("error", "啟動失敗"))
    
    result["staging_path"] = str(staging_path)
    return result


@admin_router.get("/api/tools/jp-download/{task_id}/{engine}")
async def jp_download(
    task_id: str,
    engine: str,  # google or minimax
    user: User = Depends(require_approved),
):
    """【2026-08-01 新】下載翻譯結果 .docx (限 perm 0/1)
    
    Args:
        task_id: 任務 id (從 status 拿)
        engine: google / minimax
    
    Returns:
        FileResponse with Content-Disposition: attachment (browser 觸發 Save As)
    """
    if not user or user.permission is None or user.permission > 1:
        raise HTTPException(403, "🔒 此 API 僅限家人/管理員使用")
    
    if engine not in ("google", "minimax"):
        raise HTTPException(400, f"engine 必須是 google / minimax, 收到: {engine}")
    
    from fastapi.responses import FileResponse
    from app.tools.jp_translator import get_status
    
    # Get status to find filename
    status = get_status()
    if status.get("task_id") != task_id:
        raise HTTPException(404, f"Task {task_id} 不存在或已過期")
    if status.get("state") != "done":
        raise HTTPException(400, f"Task 還沒完成 (state={status.get('state')})")
    
    # Find output file by engine suffix
    output_files = status.get("output_files", [])
    target_file = None
    for f in output_files:
        if f.endswith(f"_{engine}.docx"):
            target_file = f
            break
    
    if not target_file or not Path(target_file).exists():
        raise HTTPException(404, f"找不到 {engine} output file")
    
    return FileResponse(
        path=target_file,
        filename=Path(target_file).name,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


@admin_router.get("/api/tools/jp-status")
async def jp_status(user: User = Depends(require_approved)):
    """查詢日文翻譯目前 status (限 perm 0/1)"""
    if not user or user.permission is None or user.permission > 1:
        raise HTTPException(403, "🔒 此 API 僅限家人/管理員使用")
    from app.tools.jp_translator import get_status
    return get_status()


@admin_router.post("/api/tools/jp-reset")
async def jp_reset(user: User = Depends(require_approved)):
    """重置日文翻譯 status (限 perm 0/1)"""
    if not user or user.permission is None or user.permission > 1:
        raise HTTPException(403, "🔒 此 API 僅限家人/管理員使用")
    from app.tools.jp_translator import reset_status
    reset_status()
    return {"ok": True, "state": "idle"}


# ════════════════════════════════════════════════════════════════════════
# Batch Download Archive (Pages Split 2 漏搬的 function, Pages Split 4 補)
# ─ Classes (_ArchiveBatchItem / _ArchiveBatchRequest) 跟 function 都在這
# ════════════════════════════════════════════════════════════════════════

@admin_router.post("/api/batch-download-archive")
async def batch_download_archive(
    req: _ArchiveBatchRequest,
    request: Request,
    user: User = Depends(require_approved),
    db: AsyncSession = Depends(get_db),
):
    """
    批次下載 CAP / CEEC archive PDF → 回傳 .zip。

    跟 StudyArk batch-download 的 UX 一樣, 但:
    - 來源: 本地 archive (CAP_DIR / CEEC_DIR), 檔案已下載在 disk
    - 不撞 StudyArk 限流
    - 限 MAX_BATCH = 20 個 items (防 zip 太大)
    """
    import io
    import zipfile
    import hashlib

    MAX_BATCH = 20
    items = req.items

    if not items:
        raise HTTPException(400, "至少要 1 個 item")
    if len(items) > MAX_BATCH:
        raise HTTPException(
            400,
            f"單批最多 {MAX_BATCH} 個, 你選了 {len(items)} 個。請減少後再試。"
        )

    buffer = io.BytesIO()
    downloaded = []  # (filename, item, pdf_bytes)
    errors = []

    for idx, item in enumerate(items):
        if item.source not in ("CAP", "CEEC"):
            errors.append({"rel": item.rel, "error": f"Unknown source: {item.source}"})
            continue
        if ".." in item.rel or item.rel.startswith("/"):
            errors.append({"rel": item.rel, "error": "Invalid path"})
            continue
        base = CAP_DIR if item.source == "CAP" else CEEC_DIR
        file_path = base / item.rel
        if not file_path.exists() or not file_path.is_file():
            errors.append({"rel": item.rel, "error": "File not found"})
            continue
        if not file_path.suffix.lower() == ".pdf":
            errors.append({"rel": item.rel, "error": "Not a PDF"})
            continue
        try:
            pdf_bytes = file_path.read_bytes()
            # 驗證 PDF magic bytes
            if not pdf_bytes.startswith(b"%PDF"):
                errors.append({"rel": item.rel, "error": "Invalid PDF content"})
                continue
            downloaded.append((file_path.name, item, pdf_bytes))
        except Exception as e:
            errors.append({"rel": item.rel, "error": str(e)})

    if not downloaded and errors:
        raise HTTPException(400, f"全部 {len(items)} 個 item 都失敗: {errors[0]['error']}")

    # 寫 zip
    used_names: dict[str, int] = {}
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname, item, pdf_bytes in downloaded:
            # 若同檔名加編號避免覆蓋
            if fname in used_names:
                used_names[fname] += 1
                stem, ext = fname.rsplit(".", 1)
                zip_fname = f"{stem}_{used_names[fname]}.{ext}"
            else:
                used_names[fname] = 0
                zip_fname = fname
            zf.writestr(zip_fname, pdf_bytes)

    # 寫 DownloadHistory (每個 item)
    # 重用 DownloadHistory model, classid="CAP"/"CEEC", fileid = SHA256(rel)[:12]
    for fname, item, pdf_bytes in downloaded:
        fileid_hash = hashlib.sha256(item.rel.encode("utf-8")).hexdigest()[:12]
        dh = DownloadHistory(
            user_id=user.id,
            classid=item.source,  # "CAP" or "CEEC"
            fileid=fileid_hash,
            filetype="paper",
            title=item.title or fname,
            school_name=None,
            grade=item.grade,
            school_year=item.school_year,
            school_term=None,
            category=item.source,
            subject=item.subject,
            exam_type=None,
            version=None,
            download_filename=fname,
            ip_hash=_hash_ip(request.client.host if request.client else None),
            user_agent=request.headers.get("user-agent", "")[:512] or None,
        )
        db.add(dh)

    await log_action(
        db,
        action="batch_download_archive",
        user_id=user.id,
        target=f"items={len(downloaded)}, errors={len(errors)}, sources={set(i.source for i in items)}",
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent", ""),
    )
    await db.commit()

    from fastapi.responses import Response
    zip_name = f"tqark_archive_{len(downloaded)}_items.zip"
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{zip_name}"',
            "X-Downloaded-Count": str(len(downloaded)),
            "X-Error-Count": str(len(errors)),
        },
    )



# module import 結束時 background thread 開始跑, 完全不阻塞 startup