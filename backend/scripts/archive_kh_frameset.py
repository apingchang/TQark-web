"""
Playwright-based scraper for 高雄市 affairs.kh.edu.tw frameset schools.

Some schools (七賢, 鹽埕, 鳳甲, 立德, 燕巢) use a frameset root URL that 
wraps an iframe to /upload/upload_list/1. Static HTTP requests can only see 
the outer frameset page (12KB) but not the inner content. We need a real 
browser to render the frame and extract file_list links.

Strategy:
1. Visit root URL with Playwright headless Chromium
2. Switch to the named "main" frame
3. Extract file_list links from frame DOM
4. Enumerate file_list/{N} via direct HTTP (no Playwright needed for inner pages)
5. Download PDFs from /sites/{school_id}/upload_file/{N}/ URLs

Usage:
    uv run python scripts/archive_kh_frameset.py --school 七賢
    uv run python scripts/archive_kh_frameset.py --all  # process all frameset schools
"""
import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from playwright.sync_api import sync_playwright

ROOT = Path("/home/aping/MyProjects/TQark-web/backend")
SOURCES_FILE = ROOT / "data" / "external_sources.json"
ARCHIVE_ROOT = Path("/mnt/my_book/考題收集/_未分類")
USER_AGENT = "TQark-web/1.0"

# 段考 keyword (must match)
EXAM_KEYWORDS = ("段考", "試題", "解答", "月考", "模擬考", "補考")
# 排除 keyword
NON_EXAM_KEYWORDS = ("行事曆", "教科書", "公開授課", "收費", "代收代辦", "新生", "QA", "手冊", "演講", "研習")


def extract_school_id(root_url: str) -> str | None:
    """Parse affairs.kh.edu.tw school ID from root URL.
    
    Examples:
        https://affairs.kh.edu.tw/4338         → 4338
        https://affairs.kh.edu.tw/5555         → 5555
    """
    m = re.search(r'affairs\.kh\.edu\.tw/(\d+)', root_url)
    return m.group(1) if m else None


def extract_file_lists_with_playwright(root_url: str, max_wait_s: int = 15) -> list[str]:
    """Use Playwright to render frameset page and extract file_list/{N} links.
    
    Returns list of /sites/{school_id}/upload_file/{N}/{file} URLs.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(root_url, timeout=15000)
            page.wait_for_load_state("networkidle", timeout=max_wait_s * 1000)
            
            # Find main frame
            main_frame = next((f for f in page.frames if f.name == "main"), None)
            if not main_frame:
                print(f"  [no-main-frame]")
                return []
            
            # Extract file_list links from frame DOM
            html = main_frame.content()
            file_lists = re.findall(r'href="(?:https://affairs\.kh\.edu\.tw)?(/\d+/upload/file_list/\d+)"', html)
            unique = sorted(set(file_lists), key=lambda x: int(x.split('/')[-1]))
            
            # Also extract click handlers (some file_lists use onclick)
            onclick_links = re.findall(r"window\.location\.href='(?:https://affairs\.kh\.edu\.tw)?(/\d+/upload/file_list/\d+)'", html)
            for l in onclick_links:
                if l not in unique:
                    unique.append(l)
            
            return unique
        finally:
            browser.close()


def extract_pdfs_from_file_list(file_list_url: str) -> list[str]:
    """Visit a file_list/{N} page and extract all PDF/DOC/DOCX URLs.
    
    Uses direct HTTP (not Playwright) because file_list pages render fine without JS.
    """
    try:
        r = requests.get(file_list_url, timeout=15, headers={"User-Agent": USER_AGENT})
        if r.status_code != 200 or len(r.text) < 1000:
            return []
        pdfs = re.findall(
            r'href="(https://affairs\.kh\.edu\.tw/sites/\d+/upload_file/\d+/[^"]+\.(?:pdf|doc|docx))"',
            r.text,
        )
        return list(set(pdfs))
    except requests.RequestException:
        return []


def filter_exam_files(urls: list[str]) -> list[str]:
    """Keep only exam-related files based on filename keywords."""
    out = []
    for u in urls:
        name = u.split('/')[-1]
        if any(kw in name for kw in NON_EXAM_KEYWORDS):
            continue
        if any(kw in name for kw in EXAM_KEYWORDS):
            out.append(u)
        else:
            # include if doesn't match any filter (curriculum plans are useful too)
            out.append(u)
    return out


def download_file(url: str, target: Path, max_retries: int = 3) -> int:
    """Download file via curl (handles redirects)."""
    if target.exists() and target.stat().st_size > 1000:
        return target.stat().st_size
    target.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(max_retries):
        result = subprocess.run(
            ['curl', '-sL', '--max-time', '60', '-A', 'Mozilla/5.0', url, '-o', str(target)],
            capture_output=True,
        )
        if target.exists() and target.stat().st_size > 1000:
            return target.stat().st_size
        time.sleep(2)
    return 0


def process_frameset_school(school: dict, dry_run: bool = False) -> dict:
    """Process one frameset school."""
    name = school["name"]
    url = school["url"]
    county = school.get("county", "高雄市")
    
    school_id = extract_school_id(url)
    if not school_id:
        return {"school": name, "error": "no-school-id"}
    
    target_root = ARCHIVE_ROOT / county / name
    target_root.mkdir(parents=True, exist_ok=True)
    
    print(f"\n📁 {name} (school_id={school_id})")
    
    # Use Playwright to discover file_list URLs
    try:
        file_lists = extract_file_lists_with_playwright(url)
    except Exception as exc:
        return {"school": name, "error": f"playwright-fail: {exc}"}
    
    print(f"  file_list pages discovered: {len(file_lists)}")
    if not file_lists:
        return {"school": name, "error": "no-file-lists"}
    
    # Also try enumerate file_list 1..50 (some file_lists aren't linked from menu)
    enumerated = []
    for n in range(1, 51):
        enumerated.append(f"/{school_id}/upload/file_list/{n}")
    
    all_file_lists = list(set(file_lists + enumerated))
    print(f"  + enumerated 1-50: {len(all_file_lists)} total to scan")
    
    stats = {"downloaded": 0, "skipped": 0, "errors": 0, "files": 0}
    
    for file_list_path in all_file_lists:
        file_list_url = f"https://affairs.kh.edu.tw{file_list_path}"
        pdf_urls = extract_pdfs_from_file_list(file_list_url)
        if not pdf_urls:
            continue
        
        stats["files"] += len(pdf_urls)
        page_label = file_list_path.split('/')[-1]
        
        if dry_run:
            print(f"  file_list/{page_label}: {len(pdf_urls)} files")
            for p in pdf_urls[:2]:
                print(f"    [dry] {p.split('/')[-1]}")
            continue
        
        for pdf_url in pdf_urls:
            fname = pdf_url.split('/')[-1]
            target = target_root / fname
            if target.exists():
                stats["skipped"] += 1
                continue
            
            sz = download_file(pdf_url, target)
            if sz > 1000:
                stats["downloaded"] += 1
                # Print progress every 10 downloads
                if stats["downloaded"] % 10 == 0:
                    print(f"  [{stats['downloaded']}] {fname[:50]} ({sz:,} bytes)")
            else:
                stats["errors"] += 1
    
    print(f"  ✓ done: {stats}")
    return stats


def get_frameset_schools() -> list[dict]:
    """Get all known frameset schools from external_sources.json."""
    with open(SOURCES_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    # Heuristic: affairs.kh.edu.tw schools without /upload/upload_list/ in URL
    out = []
    for s in data["schools"]:
        url = s.get("url", "")
        if "affairs.kh.edu.tw" in url and "/upload/upload_list/" not in url:
            out.append(s)
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="Process all known frameset schools")
    parser.add_argument("--school", help="School name (override)")
    parser.add_argument("--url", help="Root URL (override)")
    parser.add_argument("--county", default="高雄市")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    
    if args.school and args.url:
        process_frameset_school({
            "name": args.school,
            "url": args.url,
            "county": args.county,
        }, dry_run=args.dry_run)
        return
    
    if args.all:
        schools = get_frameset_schools()
        print(f"📚 Found {len(schools)} frameset schools")
        for s in schools:
            process_frameset_school(s, dry_run=args.dry_run)
        return
    
    # Default: dry-run preview
    print("Usage:")
    print("  uv run python scripts/archive_kh_frameset.py --school NAME --url URL")
    print("  uv run python scripts/archive_kh_frameset.py --all")
    print("  uv run python scripts/archive_kh_frameset.py --all --dry-run")


if __name__ == "__main__":
    main()
