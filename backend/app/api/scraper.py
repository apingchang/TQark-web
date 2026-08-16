"""
StudyArk search + download endpoints(2026-07-19 改版)

設計:
- 不在 server disk 存 PDF
- 但會從 /mnt/my_book/考題收集/ 找 cache (2026-07-20 加)
- 直接從 StudyArk 抓 → stream 到 user → 寫 metadata 到 DownloadHistory
- User 透過 Content-Disposition 收到友善檔名

API:
- POST /api/search   → approved user 搜尋考古題
- GET  /api/download/{classid}/{fileid}  → 下載 PDF(stream from StudyArk)
  - querystring(可選): title, school_name, grade, school_year, school_term,
                       category, subject, exam_type, version, filetype
"""

import asyncio
import json
import zipfile
import io

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db_helpers import hash_ip, log_action
from app.core.deps import require_approved
from app.db.models import DownloadHistory, User
from app.db.session import get_db
from app.scraper import studyark

router = APIRouter(prefix="/api", tags=["scraper"])

import logging as _logging
_logger = _logging.getLogger("tqark.cache")


# ============================================================
# PDF cache LRU metadata (2026-07-20 加)
# ============================================================
from datetime import datetime, timezone, timedelta
from pathlib import Path as _Path

CACHE_META_PATH = _Path("/mnt/my_book/考題收集/state/tqark_cache_meta.json")


def _update_cache_meta(fileid: str, path: _Path) -> None:
    """寫一下 fileid 最後被 access 的時間、用來未來 LRU 判斷。"""
    try:
        if CACHE_META_PATH.exists():
            with open(CACHE_META_PATH, "r", encoding="utf-8") as f:
                meta = json.load(f)
        else:
            meta = {}
        now = datetime.now(timezone(timedelta(hours=8))).isoformat()
        entry = meta.get(fileid, {"access_count": 0})
        entry["last_access"] = now
        entry["access_count"] = entry.get("access_count", 0) + 1
        entry["path"] = str(path)
        meta[fileid] = entry
        CACHE_META_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CACHE_META_PATH, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except OSError:
        pass  # meta 寫失敗不影響 serve


class SearchRequest(BaseModel):
    grade: str | None = None
    subject: str | None = None
    school_year: str | None = None
    school_term: str | None = None
    exam_type: str | None = None
    version: str | None = None
    daan: str | None = None
    page: int = 1


async def _archive_pdf_background(
    grade: str,
    subject: str,
    filetype: str,
    fileid: str,
    pdf_bytes: bytes,
    school_year: str = "",
    school_term: str = "",
    exam_type: str = "",
    version: str = "",
    school_name: str = "",
) -> None:
    """【2026-07-26 新】背景 archive user 下載的 PDF 到 /mnt/my_book/考題收集/

    跟 /ui/download 的 cache 不同:
    - /ui/download: 1 個 PDF, 完整 OCR + county folder logic (用 cache 讀取時的 county)
    - batch download: 多個 PDF 一次進來, background archive (不 OCR, 用 fileid 命名)

    儲存結構:
        /mnt/my_book/考題收集/<grade>/<subject>/<filetype>/_tmp_<fileid>_<filetype>.pdf

    用 _tmp_<fileid> 命名跟 archive_task.py 統一 (可以讓 archive_task 看檔名知道哪些是 user 下載的)
    之後 archive_task 可以在 idle time OCR + rename 到正式 county folder。

    Args:
        grade: e.g. "四年級"
        subject: e.g. "自然"
        filetype: "paper" / "daan"
        fileid: StudyArk fileid
        pdf_bytes: 完整 PDF bytes
        其他: 保留 metadata 以後 OCR 用
    """
    import logging
    _bg_logger = logging.getLogger("tqark.bg_archive")
    try:
        if not pdf_bytes or not pdf_bytes.startswith(b"%PDF"):
            _bg_logger.info(f"[BG-ARCHIVE] {fileid}/{filetype}: not PDF, skip")
            return

        from app.scraper.archive_path import (
            build_archive_path, ensure_archive_dirs, _safe_dirname,
        )

        tmp_target = build_archive_path(
            grade, subject, filetype,
            f"_tmp_{fileid}_{filetype}",
            county=None,
        )
        ensure_archive_dirs(grade, subject, filetype, county=None)
        tmp_target.write_bytes(pdf_bytes)

        # 同時 update cache meta (LRU tracking)
        try:
            _update_cache_meta(fileid, tmp_target)
        except Exception:
            pass

        _bg_logger.info(f"[BG-ARCHIVE SAVED] {fileid}/{filetype} → {tmp_target} ({len(pdf_bytes)} bytes)")
    except OSError as e:
        # CIFS 離線 / 磁碟滿都不影響 user 下載
        _bg_logger.warning(f"[BG-ARCHIVE ERROR] {fileid}/{filetype}: {e}")
    except Exception as e:
        _bg_logger.warning(f"[BG-ARCHIVE ERROR] {fileid}/{filetype}: {e}")


@router.post("/search")
async def search(
    req: SearchRequest,
    user: User = Depends(require_approved),
    db: AsyncSession = Depends(get_db),
):
    """搜尋考古題。"""
    try:
        results = await studyark.search_papers(
            grade=req.grade,
            subject=req.subject,
            school_year=req.school_year,
            school_term=req.school_term,
            exam_type=req.exam_type,
            version=req.version,
            daan=req.daan,
            page=req.page,
        )
    except FileNotFoundError as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        raise HTTPException(502, f"StudyArk 連線失敗: {e}")

    # 撈出 list(StudyArk 格式:{list:[...], total, page, ...})
    items = []
    if isinstance(results, dict):
        items = results.get("list") or results.get("data") or results.get("results") or results.get("items") or []

    # 把結果轉成 ExamItem,順便把完整 metadata 帶在 response
    exam_items = []
    for it in items:
        try:
            exam_items.append(studyark.ExamItem.from_search_result(it, filetype="paper"))
        except Exception:
            continue

    # Audit log
    await log_action(
        db,
        action="search",
        user_id=user.id,
        target=f"grade={req.grade},subject={req.subject}",
        detail=json.dumps(req.dict(), ensure_ascii=False),
    )
    await db.commit()

    return {
        "results": results,
        "items": [
            {
                "classid": e.classid,
                "fileid": e.fileid,
                "title": e.title,
                "school_name": e.school_name,
                "grade": e.grade,
                "school_year": e.school_year,
                "school_term": e.school_term,
                "category": e.category,
                "subject": e.subject,
                "exam_type": e.exam_type,
                "version": e.version,
            }
            for e in exam_items
        ],
        "params": req.dict(),
    }


@router.get("/download/{classid}/{fileid}")
async def download(
    classid: str,
    fileid: str,
    request: Request,
    filetype: str = Query("paper", regex="^(paper|daan|answer)$"),
    # 從 search page 帶過來的完整 metadata(讓 user 下載檔名友善、log 完整)
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
    """
    下載考古題 PDF(2026-07-19 改版:不存 server,直接 stream)。

    直接從 StudyArk 抓 bytes → 用 Content-Disposition 回給 user。
    同時把 metadata 寫到 DownloadHistory(只存 metadata,不存 PDF)。
    """
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

    # 從 StudyArk 抓 bytes
    # (PDF cache 邏輯只在 /ui/download 內做 — /api/download 是內部 API、
    #  跟 batch-download 一樣走 StudyArk,不讀 archive cache)
    try:
        pdf_bytes, content_type = await studyark.download_pdf_stream(classid, fileid, filetype)
    except studyark.StudyArkRateLimit as e:
        raise HTTPException(
            status_code=429,
            detail=f"StudyArk 限流中:{e.message} 請等 {e.retry_after_minutes} 分鐘後重試。"
        )
    except FileNotFoundError as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        raise HTTPException(502, f"下載失敗: {e}")

    # 【2026-07-26 新 Phase 2】背景 archive 到 /mnt/my_book/考題收集/
    # /ui/download 會在 user 下載前 OCR county + 存正式檔名
    # /api/download 是內部 API (例如 search_results 直接下載), 背景 archive 到 _tmp_ 讓 archive_task 之後處理
    if pdf_bytes and pdf_bytes.startswith(b"%PDF") and grade and subject and filetype in ("paper", "daan"):
        asyncio.create_task(
            _archive_pdf_background(
                grade=grade,
                subject=subject,
                filetype=filetype,
                fileid=fileid,
                pdf_bytes=pdf_bytes,
                school_year=school_year or "",
                school_term=school_term or "",
                exam_type=exam_type or "",
                version=version or "",
                school_name=school_name or "",
            )
        )

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

    # 也寫 audit_log
    await log_action(
        db,
        action="download",
        user_id=user.id,
        target=f"{classid}/{fileid}",
        detail=f"filetype={filetype}; filename={filename}",
        ip=str(request.client.host) if request.client else None,
    )
    await db.commit()

    # Content-Disposition 用 RFC 5987 編碼(支援中文檔名)
    from urllib.parse import quote
    encoded_filename = quote(filename)

    headers = {
        "Content-Disposition": (
            f"attachment; "
            f"filename*=UTF-8''{encoded_filename}"  # RFC 5987 only
        ),
        "X-Download-Filename": encoded_filename,  # URL-encoded, ASCII only
    }

    return Response(
        content=pdf_bytes,
        media_type=content_type,
        headers=headers,
    )

class BatchItem(BaseModel):
    """批次下載中的單個 item"""
    classid: str
    fileid: str
    filetype: str = "paper"  # paper / daan (StudyArk API convention)
    title: str | None = None
    school_name: str | None = None
    grade: str | None = None
    school_year: str | None = None
    school_term: str | None = None
    category: str | None = None
    subject: str | None = None
    exam_type: str | None = None
    version: str | None = None


class BatchDownloadRequest(BaseModel):
    items: list[BatchItem]


class LocalBatchItem(BaseModel):
    """【2026-08-15 新】local archive batch item"""
    paper_id: str
    filetype: str = "paper"  # paper / daan


class LocalBatchDownloadRequest(BaseModel):
    items: list[LocalBatchItem]


@router.post("/batch-download")
async def batch_download(
    req: BatchDownloadRequest,
    request: Request,
    user: User = Depends(require_approved),
    db: AsyncSession = Depends(get_db),
):
    """
    批次下載多個考古題 → 回傳 .zip 包含所有 PDF。

    限制:
    - 單批最多 20 個 items(避免 timeout / 流量爆)
    - 每個 item 必須有 classid + fileid + filetype
    """
    MAX_BATCH = 20
    items = req.items

    if not items:
        raise HTTPException(400, "至少要 1 個 item")
    if len(items) > MAX_BATCH:
        raise HTTPException(
            400,
            f"單批最多 {MAX_BATCH} 個,你選了 {len(items)} 個。請減少後再試。"
        )

    # 收集 PDF bytes
    buffer = io.BytesIO()
    downloaded = []  # (filename, item)
    errors = []
    rate_limited = False  # 記錄是否有 item 撞限流
    rate_limit_msg = ""

    for idx, item in enumerate(items):
        # 【2026-07-21 改】批次中每個 item 之間休 10 秒 (從 3s 改 10s,更保守避免限流)
        # 20 個 × 10s = 200 秒 (約 3.3 分鐘) 是 user 可以接受的等待時間
        if idx > 0:
            await asyncio.sleep(10)
        try:
            pdf_bytes, _ = await studyark.download_pdf_stream(
                item.classid, item.fileid, item.filetype
            )
            # 【重點】驗證 response 真的是 PDF (magic bytes = '%PDF-')
            # StudyArk 在 batch 中碰到 anti-bot 會回 45 bytes 中文訊息
            # (例如「操作過於頻繁」、「參數錯誤」、「token 過期」)
            # 那些不含 '下載太頻繁/等待/分鐘' 關鍵字 → 原本不會 raise
            # 解法:任何不是 PDF 的 response 都視為錯誤、不寫進 zip
            if not pdf_bytes.startswith(b'%PDF'):
                text = pdf_bytes.decode('utf-8', errors='ignore')[:100]
                rate_limited = True
                rate_limit_msg = f'batch item #{idx+1} 回傳非 PDF 內容: {text}'
                errors.append({
                    "classid": item.classid,
                    "fileid": item.fileid,
                    "error": f"StudyArk 回傳非 PDF(可能是限流):{text}",
                    "retry_after_minutes": 25,
                })
                # 撞了就不要繼續 → 後面只會更慘
                break
            ei = studyark.ExamItem(
                classid=item.classid,
                fileid=item.fileid,
                filetype=item.filetype,
                title=item.title or "",
                school_name=item.school_name or "",
                grade=item.grade or "",
                school_year=item.school_year or "",
                school_term=item.school_term or "",
                category=item.category or "",
                subject=item.subject or "",
                exam_type=item.exam_type or "",
                version=item.version or "",
            )
            fname = studyark.build_download_filename(ei)
            downloaded.append((fname, item, pdf_bytes))
        except studyark.StudyArkRateLimit as e:
            rate_limited = True
            rate_limit_msg = e.message
            errors.append({
                "classid": item.classid,
                "fileid": item.fileid,
                "error": f"限流:{e.message}",
                "retry_after_minutes": e.retry_after_minutes,
            })
            # 限流就停、不要後面重複撞
            break
        except Exception as e:
            errors.append({"classid": item.classid, "fileid": item.fileid, "error": str(e)})

    if rate_limited and not downloaded:
        # 全部失敗 + 限流 → 回 429 給前端跳出友善訊息
        raise HTTPException(
            status_code=429,
            detail=f"StudyArk 限流:{rate_limit_msg} 請等幾分鐘後再試。"
        )

    # 寫 zip
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname, item, pdf_bytes in downloaded:
            # 若同檔名加編號避免覆蓋
            zf.writestr(fname, pdf_bytes)

    # 【2026-07-26 新】背景 archive 到 /mnt/my_book/考題收集/ (Phase 2)
    # batch download 每個 item 背景寫到 archive, 不攔 user zip download
    # 跟 /ui/download 的 cache 邏輯類似, 但這邊 batch 是 zip 不需要 OCR county
    # (用 classid + fileid 命名就好, 之後 /ui/download 訪問時再 OCR + rename)
    for fname, item, pdf_bytes in downloaded:
        if item.grade and item.subject and item.filetype in ("paper", "daan"):
            asyncio.create_task(
                _archive_pdf_background(
                    grade=item.grade,
                    subject=item.subject,
                    filetype=item.filetype,
                    fileid=item.fileid,
                    pdf_bytes=pdf_bytes,
                    school_year=item.school_year,
                    school_term=item.school_term,
                    exam_type=item.exam_type,
                    version=item.version,
                    school_name=item.school_name,
                )
            )

    # 寫 DownloadHistory (每個 item)
    for fname, item, pdf_bytes in downloaded:
        dh = DownloadHistory(
            user_id=user.id,
            classid=item.classid,
            fileid=item.fileid,
            filetype=item.filetype,
            title=item.title,
            school_name=item.school_name,
            grade=item.grade,
            school_year=item.school_year,
            school_term=item.school_term,
            category=item.category,
            subject=item.subject,
            exam_type=item.exam_type,
            version=item.version,
            download_filename=fname,
            ip_hash=hash_ip(request.client.host if request.client else None),
            user_agent=request.headers.get("user-agent", "")[:512] or None,
        )
        db.add(dh)

    await log_action(
        db,
        action="batch_download",
        user_id=user.id,
        target=f"{len(downloaded)} items",
        detail=f"requested={len(items)}, downloaded={len(downloaded)}, errors={len(errors)}",
        ip=str(request.client.host) if request.client else None,
    )
    await db.commit()

    # 組 zip 檔名
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_filename = f"tqark_exams_{timestamp}.zip"

    headers = {
        "Content-Disposition": f'attachment; filename="{zip_filename}"',
        "X-Batch-Count": str(len(downloaded)),
        "X-Batch-Errors": str(len(errors)),
    }

    if errors:
        headers["X-Batch-Error-Details"] = json.dumps(errors, ensure_ascii=False)[:1024]

    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers=headers,
    )


@router.post("/batch-download-local")
async def batch_download_local(
    req: LocalBatchDownloadRequest,
    request: Request,
    user: User = Depends(require_approved),
    db: AsyncSession = Depends(get_db),
):
    """【2026-08-15 新】批次下載 local archive PDF → 回傳 .zip。

    使用 paper_id 查詢 local_index、從 disk 直接讀 PDF、不撞 StudyArk 限流。
    """
    from app.scraper import local_index

    MAX_BATCH = 20
    items = req.items

    if not items:
        raise HTTPException(400, "至少要 1 個 item")
    if len(items) > MAX_BATCH:
        raise HTTPException(400, f"單批最多 {MAX_BATCH} 個, 你選了 {len(items)} 個。請減少後再試。")

    buffer = io.BytesIO()
    downloaded: list[tuple[str, dict, bytes]] = []  # (zip_fname, item, pdf_bytes)
    errors: list[dict] = []

    for it in items:
        if it.filetype not in ("paper", "daan"):
            errors.append({"paper_id": it.paper_id, "error": f"Invalid filetype: {it.filetype}"})
            continue

        group = local_index.get_paired_by_id(it.paper_id)
        if group is None:
            errors.append({"paper_id": it.paper_id, "error": "paper_id not found in local index"})
            continue

        if it.filetype == "paper":
            chosen_path = group["paper_path"]
        else:
            chosen_path = group["daan_path"]

        if not chosen_path:
            errors.append({"paper_id": it.paper_id, "error": f"no {it.filetype} file for this group"})
            continue

        p = _Path(chosen_path)
        if not p.exists() or not p.is_file():
            errors.append({"paper_id": it.paper_id, "error": f"file missing on disk: {chosen_path}"})
            continue

        try:
            pdf_bytes = p.read_bytes()
            if not pdf_bytes.startswith(b"%PDF"):
                errors.append({"paper_id": it.paper_id, "error": "not a valid PDF"})
                continue
            # zip 內的檔名
            title_parts = [group["school_name"], group["school_year"] + ("學年度" if group["school_year"] else ""),
                           group["school_term"], group["exam_type"], group["grade"], group["subject"]]
            if group["version"] and group["version"] not in ("未註明", ""):
                title_parts.append(f"({group['version']})")
            title_parts.append("答案" if it.filetype == "daan" else "試題")
            zip_fname = "_".join([t for t in title_parts if t]).replace("/", "／").replace(":", "：") + ".pdf"
            downloaded.append((zip_fname, {"paper_id": it.paper_id, "filetype": it.filetype, "group": group}, pdf_bytes))
        except Exception as e:
            errors.append({"paper_id": it.paper_id, "error": str(e)})

    if not downloaded and errors:
        raise HTTPException(400, f"全部 {len(items)} 個 item 都失敗: {errors[0]['error']}")

    # 寫 zip (去重)
    used: dict[str, int] = {}
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname, item, pdf_bytes in downloaded:
            if fname in used:
                used[fname] += 1
                stem, ext = fname.rsplit(".", 1)
                actual = f"{stem}_{used[fname]}.{ext}"
            else:
                used[fname] = 0
                actual = fname
            zf.writestr(actual, pdf_bytes)

    # 寫 DownloadHistory
    for fname, item, pdf_bytes in downloaded:
        group = item["group"]
        dh = DownloadHistory(
            user_id=user.id,
            classid="LOCAL",
            fileid=item["paper_id"],
            filetype=item["filetype"],
            title=group["title"] or fname,
            school_name=group["school_name"] or None,
            grade=group["grade"] or None,
            school_year=group["school_year"] or None,
            school_term=group["school_term"] or None,
            category=None,
            subject=group["subject"] or None,
            exam_type=group["exam_type"] or None,
            version=group["version"] or None,
            download_filename=fname,
            ip_hash=hash_ip(request.client.host if request.client else None),
            user_agent=request.headers.get("user-agent", "")[:512] or None,
        )
        db.add(dh)

    await log_action(
        db,
        action="batch_download_local",
        user_id=user.id,
        target=f"{len(downloaded)} items",
        detail=f"requested={len(items)}, downloaded={len(downloaded)}, errors={len(errors)}",
        ip=str(request.client.host) if request.client else None,
    )
    await db.commit()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_filename = f"tqark_local_exams_{timestamp}.zip"
    headers = {
        "Content-Disposition": f'attachment; filename="{zip_filename}"',
        "X-Batch-Count": str(len(downloaded)),
        "X-Batch-Errors": str(len(errors)),
    }
    if errors:
        headers["X-Batch-Error-Details"] = json.dumps(errors, ensure_ascii=False)[:1024]

    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers=headers,
    )
