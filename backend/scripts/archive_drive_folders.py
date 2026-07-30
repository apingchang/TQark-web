#!/usr/bin/env python3
"""
Google Drive folder archive (2026-07-30)

任務: 從 external_sources.json 的 32 個 link_type=drive folder 抓所有 PDF/DOC
- 使用 Google Drive API + API Key (public folders only)
- 寫到 StudyArk 結構:
    /mnt/my_book/考題收集/<county>/<level>/<grade>/<subject>/<paper|daan>/<file>

Usage:
  source .venv/bin/activate
  uv run python scripts/archive_drive_folders.py --folder-id 1M2rPpBNoSnqwGg_Nx9yMsWPWu5wh32tH
  uv run python scripts/archive_drive_folders.py --all
  uv run python scripts/archive_drive_folders.py --dry-run --all
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv  # noqa
import requests

# === Paths ===
ROOT = Path("/home/aping/MyProjects/TQark-web")
ARCHIVE_ROOT = Path("/mnt/my_book/考題收集")
SOURCES_FILE = ROOT / "backend" / "data" / "external_sources.json"

# Load .env
load_dotenv(ROOT / "credentials" / ".env")
API_KEY = os.environ.get("GOOGLE_DRIVE_API_KEY", "")

# Drive API endpoint
DRIVE_API = "https://www.googleapis.com/drive/v3/files"

# File extensions to download
DOWNLOAD_EXTENSIONS = {".pdf", ".doc", ".docx"}

# 2026-07-30: require exam-like keywords OR explicit allowlist
EXAM_KEYWORDS = ("段考", "試題", "考題", "解答", "答案", "考卷", "試卷", "考古")
# Skip these (admin/行政)
NON_EXAM_KEYWORDS = (
    "申請", "配置圖", "平面圖", "校服", "校旗", "手冊",
    "流程", "辦法", "規定", "原則", "行事曆", "教科書",
    "校務", "會議", "研習", "簽到", "回條", "報名", "家長",
    "審題表", "雙向細目", "題庫", "命題", "空白試卷", "空白卷", "校用",
)
# Skip image/non-exam
SKIP_MIME_PREFIX = ("image/", "video/", "audio/")

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("drive-archive")


def load_schools() -> list[dict]:
    """Load drive-folder schools from external_sources.json."""
    data = json.load(open(SOURCES_FILE))
    return [s for s in data.get("schools", []) if s.get("link_type") == "drive"]


def folder_id_from_url(url: str) -> str | None:
    """Extract folder ID from Drive URL."""
    m = re.search(r"/folders/([a-zA-Z0-9_-]+)", url)
    return m.group(1) if m else None


def list_folder_files(folder_id: str, page_token: str | None = None) -> tuple[list[dict], str | None]:
    """List all files in a public Drive folder via API."""
    params = {
        "key": API_KEY,
        "q": f"'{folder_id}' in parents and trashed = false",
        "fields": "nextPageToken,files(id,name,mimeType,size,webContentLink)",
        "pageSize": 1000,
    }
    if page_token:
        params["pageToken"] = page_token
    response = requests.get(DRIVE_API, params=params, timeout=30)
    if response.status_code != 200:
        err = response.json().get("error", {}).get("message", response.text[:200])
        raise RuntimeError(f"List failed: {response.status_code} {err}")
    data = response.json()
    return data.get("files", []), data.get("nextPageToken")


def download_file(web_url: str, target_path: Path, max_retries: int = 3) -> int:
    """Download a public Drive file via webContentLink (no API key needed).
    
    2026-07-30: switched from files.get?alt=media (which 403-rate-limited)
    to public webContentLink path `drive.google.com/uc?id=...&export=download`.
    This bypasses API quota limits for public files entirely.
    """
    headers = {"User-Agent": "TQark-web/1.0"}
    last_err = None
    for attempt in range(max_retries):
        try:
            response = requests.get(web_url, headers=headers, timeout=120, stream=True, allow_redirects=True)
            if response.status_code == 200:
                target_path.parent.mkdir(parents=True, exist_ok=True)
                size = 0
                with target_path.open("wb") as f:
                    for chunk in response.iter_content(chunk_size=64 * 1024):
                        f.write(chunk)
                        size += len(chunk)
                return size
            if response.status_code == 403:
                wait = 5 * (attempt + 1)
                logger.info("  [web 403, sleeping %ds, attempt %d/%d]",
                            wait, attempt + 1, max_retries)
                time.sleep(wait)
                continue
            raise RuntimeError(f"Download failed: {response.status_code} {response.headers.get('content-type','?')[:60]}")
        except (requests.RequestException, IOError) as exc:
            last_err = exc
            time.sleep(2)
    raise RuntimeError(f"Download failed after {max_retries} attempts: {last_err}")


def is_exam_file(name: str, mime_type: str = "", parent_path: str = "") -> bool:
    """Heuristic: filter for exam-like filenames.

    2026-07-30: require exam-like keywords in NAME OR parent folder path.
    Many school Drive folders use subject-only filenames (e.g. "8英文A.pdf")
    inside a "110學年第一期段考" subfolder — need to honor ancestor context.
    """
    if any(mime_type.startswith(p) for p in SKIP_MIME_PREFIX):
        return False
    ext = Path(name).suffix.lower()
    if ext not in DOWNLOAD_EXTENSIONS:
        return False
    if any(kw in name for kw in NON_EXAM_KEYWORDS):
        return False
    if any(kw in name for kw in EXAM_KEYWORDS):
        return True
    # 【2026-07-30】parent path keywords indicate段考 context
    return any(kw in parent_path for kw in ("段考", "段試", "試題", "考題", "考古", "考卷", "試卷", "模擬", "期末"))


def target_path_for(school: dict, filename: str, sub_path: str = "") -> Path:
    """Map filename to StudyArk structure based on school record.

    sub_path is the breadcrumb of subfolder names e.g. "114學年_第2學期_段考"
    """
    county = school.get("county", "未分類")
    # 2026-07-30: category is "junior"/"senior"/"elementary" - map to 中文
    cat = school.get("category", "junior")
    level_map = {"junior": "國中", "senior": "高中", "elementary": "國小"}
    level = level_map.get(cat, cat)
    base = ARCHIVE_ROOT / county / level / "_DriveFolder" / school["name"]
    if sub_path:
        return base / sub_path / filename
    return base / filename


# Drive folder MIME type
DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"


def walk_folder(school: dict, folder_id: str, path_segments: list[str],
                depth: int, max_depth: int, stats: dict, dry_run: bool,
                seen: set[str]) -> None:
    """Recursively walk a folder, downloading exam files.
    
    sub_folder names are appended to path_segments e.g. ["114學年度第二學期"]
    """
    if folder_id in seen or depth > max_depth:
        return
    seen.add(folder_id)

    page_token = None
    while True:
        try:
            files, page_token = list_folder_files(folder_id, page_token)
        except Exception as exc:
            logger.warning("  [list-error] %s: %s", "/".join(path_segments), exc)
            stats["errors"] += 1
            stats["error_msgs"].append(str(exc))
            return

        for f in files:
            fname = f.get("name", "")
            mime = f.get("mimeType", "")
            stats["files"] += 1

            if mime == DRIVE_FOLDER_MIME:
                # Recurse into subfolder
                walk_folder(school, f["id"], path_segments + [fname],
                            depth + 1, max_depth, stats, dry_run, seen)
                continue

            if not is_exam_file(fname, mime, "/".join(path_segments[1:])):
                stats["skipped"] += 1
                continue

            sub_path = "/".join(path_segments[1:])  # drop first segment (school name placeholder)
            target = target_path_for(school, fname, sub_path)
            if target.exists():
                logger.info("  [exists] %s", target.relative_to(ARCHIVE_ROOT))
                stats["skipped"] += 1
                continue

            if dry_run:
                logger.info("  [dry-run] %s", target.relative_to(ARCHIVE_ROOT))
                stats["downloaded"] += 1
                continue

            try:
                # 【2026-07-30】Use webContentLink (public URL, no API quota)
                web = f.get("webContentLink")
                if not web:
                    # Fallback: build URL manually
                    web = f"https://drive.google.com/uc?id={f['id']}&export=download"
                size = download_file(web, target)
                stats["downloaded"] += 1
                logger.info("  [downloaded] %s (%d KB)",
                            target.relative_to(ARCHIVE_ROOT), size // 1024)
                time.sleep(0.5)  # gentle pacing
            except Exception as exc:
                stats["errors"] += 1
                stats["error_msgs"].append(f"{fname}: {exc}")
                logger.warning("  [dl-error] %s: %s", fname, exc)

        if not page_token:
            break


def archive_school(school: dict, dry_run: bool = False, max_depth: int = 5) -> dict:
    """Archive one school's Drive folder recursively. Returns stats dict."""
    name = school["name"]
    folder_id = folder_id_from_url(school["url"])
    stats = {"school": name, "folder_id": folder_id, "files": 0, "downloaded": 0,
             "skipped": 0, "errors": 0, "error_msgs": []}

    if not folder_id:
        stats["errors"] += 1
        stats["error_msgs"].append("no folder ID")
        return stats

    logger.info("📁 %s (folder=%s, max_depth=%d)", name, folder_id, max_depth)
    walk_folder(school, folder_id, [name], depth=0, max_depth=max_depth,
                stats=stats, dry_run=dry_run, seen=set())

    return stats


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--folder-id", help="Single Drive folder ID")
    parser.add_argument("--all", action="store_true", help="All 32 drive schools")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="Max schools (0=all)")
    args = parser.parse_args()

    if not API_KEY:
        logger.error("❌ GOOGLE_DRIVE_API_KEY not set in .env")
        logger.error("   See: backend/credentials/.env.example")
        sys.exit(1)

    schools = load_schools()
    if args.folder_id:
        target = [s for s in schools if folder_id_from_url(s["url"]) == args.folder_id]
        if not target:
            # Allow ad-hoc folder ID
            target = [{"name": f"Ad-hoc {args.folder_id}", "url": f"https://drive.google.com/drive/folders/{args.folder_id}",
                       "county": "未分類", "category": "國中"}]
        schools = target
    elif not args.all:
        parser.print_help()
        logger.info("\n💡 Use --folder-id XXXX  or  --all")
        sys.exit(0)

    if args.limit:
        schools = schools[:args.limit]

    logger.info("📚 Drive archive: %d schools (dry_run=%s)", len(schools), args.dry_run)
    total = {"files": 0, "downloaded": 0, "skipped": 0, "errors": 0, "error_msgs": []}
    for s in schools:
        stats = archive_school(s, dry_run=args.dry_run)
        for k in total:
            total[k] += stats[k] if isinstance(stats[k], list) else stats[k]
        logger.info("  ➜ files=%d downloaded=%d skipped=%d errors=%d",
                    stats["files"], stats["downloaded"], stats["skipped"], stats["errors"])

    logger.info("\n=== Summary ===")
    logger.info("Schools: %d   Total files seen: %d", len(schools), total["files"])
    logger.info("Downloaded: %d   Skipped: %d   Errors: %d",
                total["downloaded"], total["skipped"], total["errors"])


if __name__ == "__main__":
    main()
