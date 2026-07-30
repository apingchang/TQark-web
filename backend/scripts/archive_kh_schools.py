"""
Generic scraper for 高雄市國中公開段考 PDF.
Pattern: https://www.{school}.kh.edu.tw/view/index.php?WebID=XXX&MainType=0&SubType=104&...

Strategy:
1. Visit the page (MainType=0&SubType=104 is usually 段考試題 menu)
2. Parse all /upload/{WebID}/104_{SubMenuId}/*.pdf links
3. Filter to keyword (段考 / 試題 / 解答)
4. Download to _未分類/{county}/{school}/

Usage:
    uv run python scripts/archive_kh_schools.py --school 大灣國中 --url https://www.dwm.kh.edu.tw/view/index.php?WebID=344...
    uv run python scripts/archive_kh_schools.py --auto  # use schools from external_sources.json with link_type=kh_web
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urljoin

import requests

ROOT = Path("/home/aping/MyProjects/TQark-web/backend")
SOURCES_FILE = ROOT / "data" / "external_sources.json"
ARCHIVE_ROOT = Path("/mnt/my_book/考題收集/_未分類")
USER_AGENT = "TQark-web/1.0"

# 段考 keyword (must match)
EXAM_KEYWORDS = ("段考", "試題", "解答", "月考", "模擬考", "補考")
# 排除 keyword
NON_EXAM_KEYWORDS = ("行事曆", "教科書", "公開授課", "收費", "代收代辦", "新生", "QA", "手冊", "演講", "研習")


def extract_pdfs(html: str) -> list[tuple[str, str]]:
    """Return list of (absolute_url, filename) pairs from a kh.edu.tw page."""
    pdfs = re.findall(r'href="(/upload/[^"]+\.pdf)"', html)
    seen = set()
    out = []
    for p in pdfs:
        if p in seen:
            continue
        seen.add(p)
        out.append((p, p.split("/")[-1]))
    return out


def filter_exam(files: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Keep only exam-related PDFs."""
    out = []
    for url, name in files:
        if any(kw in name for kw in NON_EXAM_KEYWORDS):
            continue
        if any(kw in name for kw in EXAM_KEYWORDS):
            out.append((url, name))
    return out


def download(url: str, target: Path, max_retries: int = 3) -> int:
    """Download file via curl."""
    if target.exists():
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


def fetch_page(url: str) -> str | None:
    """Fetch HTML page."""
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": USER_AGENT})
        if r.status_code == 200:
            return r.text
    except requests.RequestException as exc:
        print(f"  [fetch-error] {exc}")
    return None


def process_school(school: dict, dry_run: bool = False) -> dict:
    """Process one school's kh.edu.tw page."""
    name = school["name"]
    url = school["url"]
    county = school.get("county", "未分類")
    
    target_root = ARCHIVE_ROOT / county / name
    target_root.mkdir(parents=True, exist_ok=True)
    
    print(f"\n📁 {name} ({url})")
    html = fetch_page(url)
    if not html:
        return {"school": name, "error": "fetch-failed"}
    
    pdfs = extract_pdfs(html)
    exam_pdfs = filter_exam(pdfs)
    
    print(f"  Total PDFs: {len(pdfs)}, exam-relevant: {len(exam_pdfs)}")
    
    stats = {"downloaded": 0, "skipped": 0, "errors": 0, "files": len(exam_pdfs)}
    
    if not dry_run:
        for path, fname in exam_pdfs:
            abs_url = urljoin(url, path)
            target = target_root / fname
            if target.exists():
                stats["skipped"] += 1
                continue
            try:
                sz = download(abs_url, target)
                if sz > 1000:
                    stats["downloaded"] += 1
                    print(f"  ✓ {fname} ({sz:,} bytes)")
                else:
                    stats["errors"] += 1
                    print(f"  ✗ {fname}")
            except Exception as exc:
                stats["errors"] += 1
                print(f"  ✗ {fname}: {exc}")
    
    return stats


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--auto", action="store_true",
                        help="Process all link_type=kh_web schools from external_sources.json")
    parser.add_argument("--school", help="School name (override)")
    parser.add_argument("--url", help="Page URL (override)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    with open(SOURCES_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    if args.school and args.url:
        process_school({"name": args.school, "url": args.url, "county": "高雄市"}, args.dry_run)
        return
    
    if args.auto:
        kh_schools = [s for s in data["schools"] if s.get("link_type") == "kh_web"]
        print(f"📚 Found {len(kh_schools)} kh_web schools")
        for s in kh_schools:
            process_school(s, args.dry_run)
        return
    
    # default: dry-run preview
    print("Usage:")
    print("  uv run python scripts/archive_kh_schools.py --school NAME --url URL")
    print("  uv run python scripts/archive_kh_schools.py --auto")
    print("  uv run python scripts/archive_kh_schools.py --auto --dry-run")


if __name__ == "__main__":
    main()


def process_affairs_school(school: dict, max_n: int = 300, dry_run: bool = False) -> dict:
    """Process a school using affairs.kh.edu.tw/upload/file_list/{N} pattern.
    
    The affairs.kh.edu.tw platform hosts many 高雄市 schools' 試題 archives.
    Each school has multiple file_list/{N} sub-pages containing PDFs.
    """
    url = school["url"]
    name = school["name"]
    county = school.get("county", "高雄市")
    
    # Parse school ID from URL
    # e.g. https://affairs.kh.edu.tw/5365/upload/upload_list/2 → school_id=5365
    m = re.search(r'affairs\.kh\.edu\.tw/(\d+)/upload/upload_list/(\d+)', url)
    if not m:
        return {"school": name, "error": "url-pattern-fail"}
    
    school_id = m.group(1)
    list_id = m.group(2)
    
    target_root = ARCHIVE_ROOT / county / name
    target_root.mkdir(parents=True, exist_ok=True)
    
    print(f"\n📁 {name} (school_id={school_id})")
    
    total_stats = {"downloaded": 0, "skipped": 0, "errors": 0, "files": 0}
    
    # Enumerate file_list 1..max_n
    for n in range(1, max_n + 1):
        page_url = f"https://affairs.kh.edu.tw/{school_id}/upload/file_list/{n}"
        try:
            r = requests.get(page_url, timeout=15, headers={"User-Agent": USER_AGENT})
        except requests.RequestException:
            continue
        
        if r.status_code != 200 or len(r.text) < 5000:
            continue
        
        # Find PDF/DOC links
        pdfs = re.findall(r'href="(https://affairs\.kh\.edu\.tw/sites/\d+/upload_file/\d+/[^"]+\.(?:pdf|doc|docx))"', r.text)
        if not pdfs:
            continue
        
        print(f"  file_list/{n}: {len(pdfs)} files")
        total_stats["files"] += len(pdfs)
        
        if dry_run:
            for p in pdfs[:3]:
                print(f"    [dry] {p.split('/')[-1]}")
            continue
        
        for pdf_url in pdfs:
            fname = pdf_url.split('/')[-1]
            target = target_root / fname
            if target.exists():
                total_stats["skipped"] += 1
                continue
            
            try:
                r2 = requests.get(pdf_url, timeout=60, headers={"User-Agent": USER_AGENT})
                if r2.status_code == 200 and len(r2.content) > 1000:
                    target.write_bytes(r2.content)
                    total_stats["downloaded"] += 1
                    print(f"  ✓ {fname} ({len(r2.content):,} bytes)")
                else:
                    total_stats["errors"] += 1
                    print(f"  ✗ {fname}: HTTP {r2.status_code}")
            except Exception as exc:
                total_stats["errors"] += 1
                print(f"  ✗ {fname}: {exc}")
    
    return total_stats


