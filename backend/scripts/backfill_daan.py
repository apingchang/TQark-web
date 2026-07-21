#!/usr/bin/env python3
"""Backfill 答案卷: 對每個 collected fileid, 確認 daan 存在並下載。

用法:
    uv run python scripts/backfill_daan.py [--limit 50]
"""
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from datetime import datetime

sys.path.insert(0, ".")

from app.scraper import studyark
from app.scraper.studyark import StudyArkRateLimit, load_cookies

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("backfill")

ARCHIVE_ROOT = Path("/mnt/my_book/考題收集")


def get_existing_daan_paths(fileid: str) -> set:
    """找該 fileid 的 daan PDF 路徑 (不管 county/folder)"""
    found = set()
    for path in ARCHIVE_ROOT.rglob(f"*_{fileid}_*daan*.pdf"):
        found.add(path)
    for path in ARCHIVE_ROOT.rglob(f"*_{fileid}_*_daan.pdf"):
        found.add(path)
    return found


async def fetch_metadata(fileid: str) -> dict | None:
    """從 search response 找 fileid 的 metadata (翻多頁)"""
    for grade in [None, "一年級", "二年級", "三年級", "四年級", "五年級", "六年級",
                   "七年級", "八年級", "九年級"]:
        for page in range(1, 100):
            try:
                result = await studyark.search_papers(grade=grade, page=page)
            except StudyArkRateLimit:
                raise
            except Exception as e:
                log.warning(f"  search error grade={grade} page={page}: {e}")
                continue
            for item in result.get("list", []):
                if str(item["id"]) == fileid:
                    return item
            if page >= result.get("total_page", 1):
                break
    return None


async def download_daan(item: dict) -> bytes | None:
    classid = str(item.get("classid", ""))
    fileid = str(item.get("id", ""))
    try:
        pdf_bytes, _ = await studyark.download_pdf_stream(
            classid=classid, fileid=fileid, filetype="daan",
        )
        return pdf_bytes
    except Exception as e:
        log.warning(f"  daan download error: {e}")
        return None


async def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=200, help="Max fileids to process")
    parser.add_argument("--delay", type=float, default=2.0, help="Delay between requests")
    args = parser.parse_args()
    
    # Load collected fileids
    archive_status = json.load(open("/mnt/my_book/考題收集/state/archive_status.json"))
    collected = archive_status.get("collected_fileids", [])
    log.info(f"Total collected fileids: {len(collected)}")
    
    success = 0
    skipped = 0
    failed = 0
    no_answer = 0
    
    for i, fileid in enumerate(collected[:args.limit]):
        existing = get_existing_daan_paths(fileid)
        if existing:
            skipped += 1
            continue
        
        # Get metadata
        try:
            item = await fetch_metadata(fileid)
        except StudyArkRateLimit as e:
            log.warning(f"  Rate limited! Sleeping 5 min then retry...")
            await asyncio.sleep(300)
            continue
        
        if not item:
            log.warning(f"  fileid={fileid}: not found in search")
            failed += 1
            continue
        
        if item.get("download_answer") != "有":
            no_answer += 1
            continue
        
        # Download daan
        log.info(f"  [{i+1}/{len(collected)}] fileid={fileid}: download daan ({item.get('title', '')[:30]})")
        daan_bytes = await download_daan(item)
        
        if daan_bytes and daan_bytes.startswith(b"%PDF"):
            # Find corresponding paper file to determine path
            paper_paths = list(ARCHIVE_ROOT.rglob(f"*_{fileid}_*.pdf"))
            paper_paths = [p for p in paper_paths if "/paper/" in str(p) or "/_未分類/" in str(p)]
            
            if not paper_paths:
                log.warning(f"  fileid={fileid}: no paper file found, skipping")
                failed += 1
                continue
            
            paper_path = paper_paths[0]
            daan_dir = paper_path.parent.parent / "daan"
            daan_dir.mkdir(parents=True, exist_ok=True)
            
            # Reuse filename but with _daan instead of _paper
            filename = paper_path.name.replace("/paper/", "/daan/")
            daan_path = daan_dir / paper_path.name.replace("paper", "daan")
            
            daan_path.write_bytes(daan_bytes)
            log.info(f"    ✓ saved: {daan_path.relative_to(ARCHIVE_ROOT)} ({len(daan_bytes)} bytes)")
            success += 1
        else:
            failed += 1
        
        await asyncio.sleep(args.delay)
        
        if (i + 1) % 10 == 0:
            log.info(f"  Progress: {i+1}/{len(collected)}, success={success}, skipped={skipped}, no_answer={no_answer}, failed={failed}")
    
    log.info(f"\n=== Done ===")
    log.info(f"Success: {success}, Skipped (already exists): {skipped}, No answer: {no_answer}, Failed: {failed}")


if __name__ == "__main__":
    asyncio.run(main())
