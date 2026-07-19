"""
StudyArk search + download endpoints

- POST /api/search   → approved user 搜尋考古題
- GET  /api/download/{exam_id}  → 下載 PDF(會先 cache)
"""

from fastapi import APIRouter, Depends, Form, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db_helpers import log_action
from app.core.deps import require_approved
from app.db.models import User
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
    """
    搜尋考古題。
    需要 approved 狀態才能用。
    """
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

    # Audit log
    await log_action(
        db,
        action="search",
        user_id=user.id,
        target=f"grade={req.grade},subject={req.subject}",
        detail=json.dumps(req.dict()) if hasattr(req, "dict") else None,
    )
    await db.commit()

    return {
        "results": results,
        "params": req.dict(),
    }


@router.get("/download/{classid}/{fileid}")
async def download(
    classid: str,
    fileid: str,
    filetype: str = Query("paper", regex="^(paper|answer)$"),
    user: User = Depends(require_approved),
    db: AsyncSession = Depends(get_db),
):
    """
    下載考古題 PDF。
    - paper: 試卷
    - answer: 答案

    先檢查 cache,有就直接回;沒有就抓並存 cache。
    """
    try:
        fpath = await studyark.download_to_cache(classid, fileid, filetype)
    except FileNotFoundError as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        raise HTTPException(502, f"下載失敗: {e}")

    # Audit log
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


import json  # for log_action detail serialization