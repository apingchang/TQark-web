"""
StudyArk search + download endpoints(2026-07-19 改版)

設計:
- 不在 server disk 存 PDF
- 直接從 StudyArk 抓 → stream 到 user → 寫 metadata 到 DownloadHistory
- User 透過 Content-Disposition 收到友善檔名

API:
- POST /api/search   → approved user 搜尋考古題
- GET  /api/download/{classid}/{fileid}  → 下載 PDF(stream from StudyArk)
  - querystring(可選): title, school_name, grade, school_year, school_term,
                       category, subject, exam_type, version, filetype
"""

import json

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


class SearchRequest(BaseModel):
    grade: str | None = None
    subject: str | None = None
    school_year: str | None = None
    school_term: str | None = None
    exam_type: str | None = None
    version: str | None = None
    daan: str | None = None
    page: int = 1


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
    filetype: str = Query("paper", regex="^(paper|answer)$"),
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