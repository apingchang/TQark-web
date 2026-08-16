#!/usr/bin/env python3
"""Archive exam files from school-owned ``.edu.tw`` websites.

The source list comes from ``data/external_sources.json``.  Each discovered
PDF, Office document, or ZIP member is written to the same StudyArk directory
layout used by the other archive scripts::

    <county>/<level>/<grade>/<subject>/<paper|daan>/<filename>

Usage:
    uv run python scripts/scrape_edu_tw_schools.py --dry-run --limit 5
    uv run python scripts/scrape_edu_tw_schools.py --limit 10
    uv run python scripts/scrape_edu_tw_schools.py --force --match 板橋
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import re
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from migrate_tcool_to_studyark_structure import (  # noqa: E402
    ARCHIVE_ROOT,
    parse_filename,
)

SOURCE_FILE = BACKEND_DIR / "data" / "external_sources.json"
STATE_FILE = ARCHIVE_ROOT / "logs" / "edu_tw_status.json"
LOG_FILE = ARCHIVE_ROOT / "logs" / "edu_tw_schools.log"

FILE_EXTENSIONS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip"}
ZIP_MEMBER_EXTENSIONS = FILE_EXTENSIONS - {".zip"}
LEVEL_BY_CATEGORY = {
    "elementary": "國小",
    "junior": "國中",
    "senior": "高中",
}
NAVIGATION_KEYWORDS = (
    "考古",
    "段考",
    "試題",
    "考卷",
    "題庫",
    "學期",
    "下載",
    "補考",
)
NON_EXAM_FILE_KEYWORDS = (
    "申請",
    "配置圖",
    "平面圖",
    "校服",
    "校旗",
    "手冊",
    "流程",
    "辦法",
    "規定",
    "原則",
    "行事曆",
    "教科書",
)
USER_AGENT = "TQark-web/1.0"
REQUEST_TIMEOUT = 30
PARSER_TIMEOUT_MS = 10_000
MAX_CRAWL_PAGES = 60
MAX_CRAWL_DEPTH = 5
PLAYWRIGHT_PAGES_PER_BROWSER = 8


@dataclass(frozen=True)
class FileLink:
    """A downloadable file and the page context where it was found."""

    url: str
    label: str
    page_title: str


def setup_logging() -> logging.Logger:
    """Configure console and archive-file logging."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("edu_tw_archive")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "[%(asctime)s] %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    return logger


def load_schools() -> list[dict[str, Any]]:
    """Load school-owned ``.edu.tw`` sources, accepting both schema names."""
    data = json.loads(SOURCE_FILE.read_text(encoding="utf-8"))
    schools = data.get("schools", data if isinstance(data, list) else [])
    selected = []
    for school in schools:
        link_type = school.get("type") or school.get("link_type")
        hostname = (urlparse(school.get("url", "")).hostname or "").lower()
        if link_type == "school_web" and ".edu.tw" in hostname:
            selected.append(school)
    return selected


def load_state() -> dict[str, Any]:
    """Read resumable per-school state, tolerating a missing/corrupt file."""
    if not STATE_FILE.exists():
        return {"version": 1, "schools": {}}
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "schools": {}}
    if "schools" not in state:
        state = {"version": 1, "schools": state}
    return state


def save_state(state: dict[str, Any]) -> None:
    """Atomically persist scraper state."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(STATE_FILE)


def safe_filename(name: str) -> str:
    """Return a filesystem-safe basename."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", unquote(name)).strip(" .")
    return cleaned[:220] or "unnamed"


def file_extension(value: str) -> str:
    """Extract a supported extension from a URL, label, or filename."""
    parsed = urlparse(value)
    candidates = [parsed.path, value]
    query = parse_qs(parsed.query)
    candidates.extend(item for values in query.values() for item in values)
    for candidate in candidates:
        match = re.search(r"\.(pdf|docx?|xlsx?|zip)(?:$|[?&#/])", candidate, re.I)
        if match:
            return f".{match.group(1).lower()}"
    return ""


def is_file_link(url: str, label: str = "") -> bool:
    """Return whether an anchor looks like a supported download."""
    return bool(file_extension(url) or file_extension(label))


def choose_filename(link: FileLink) -> str:
    """Prefer a descriptive anchor label over an opaque URL basename."""
    extension = file_extension(link.url) or file_extension(link.label)
    url_name = Path(unquote(urlparse(link.url).path)).name
    label = re.sub(r"\s+", " ", link.label).strip()
    label_is_descriptive = len(Path(label).stem) >= 4 and label not in {
        "下載",
        "檔案下載",
        "download",
    }
    candidate = label if label_is_descriptive else url_name
    candidate = safe_filename(candidate)
    if extension and Path(candidate).suffix.lower() not in FILE_EXTENSIONS:
        candidate += extension
    if not Path(candidate).suffix and extension:
        candidate += extension
    return candidate


def extract_links(html: str, page_url: str) -> tuple[list[FileLink], list[tuple[str, str]]]:
    """Extract download and navigation anchors from one HTML page."""
    soup = BeautifulSoup(html, "html.parser")
    page_title = soup.title.get_text(" ", strip=True) if soup.title else ""
    files: list[FileLink] = []
    navigation: list[tuple[str, str]] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "").strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        full_url = urljoin(page_url, href)
        label = anchor.get_text(" ", strip=True)
        if is_file_link(full_url, label):
            files.append(FileLink(full_url, label, page_title))
        else:
            navigation.append((full_url, label))
    return files, navigation


def strategy_for(url: str) -> str:
    """Classify a source URL into the least expensive suitable strategy."""
    parsed = urlparse(url)
    path = parsed.path.lower()
    if re.search(r"/p/[^/]+\.php$", path):
        return "playwright"
    if "/view/index.php" in path:
        return "kh_view"
    if "/upload/" in path:
        return "kh_upload"
    if "/modules/" in path:
        return "modules"
    if "/nss/" in path:
        return "nss"
    return "static"


def should_follow(
    strategy: str,
    start_url: str,
    candidate_url: str,
    label: str,
) -> bool:
    """Keep recursive crawling on the same relevant CMS section."""
    start = urlparse(start_url)
    candidate = urlparse(candidate_url)
    if candidate.hostname != start.hostname:
        return False

    path = candidate.path.lower()
    if strategy == "kh_view":
        start_query = {key.lower(): value for key, value in parse_qs(start.query).items()}
        candidate_query = {
            key.lower(): value for key, value in parse_qs(candidate.query).items()
        }
        return (
            path == start.path.lower()
            and candidate_query.get("mainmenuid") == start_query.get("mainmenuid")
            and "submenuid" in candidate_query
        )
    if strategy == "kh_upload":
        return "/upload/file_list/" in path or "/upload/upload_list/" in path
    if strategy == "modules":
        # 【2026-07-29 fix】follow 子類別 (e.g. 7/8/9 年級 不同的 of_cat_sn)
        if path != start.path.lower():
            return False
        if not parse_qs(candidate.query).get("of_cat_sn"):
            return False
        return any(
            keyword in f"{label} {candidate.query}" for keyword in NAVIGATION_KEYWORDS
        ) or "tad_uploader" in path or "list_mode" in candidate.query
    if strategy == "nss":
        return path.startswith(start.path.rstrip("/") + "/") and any(
            keyword in label for keyword in NAVIGATION_KEYWORDS
        )
    return any(keyword in label for keyword in NAVIGATION_KEYWORDS)


def deduplicate_links(links: list[FileLink]) -> list[FileLink]:
    """Deduplicate downloads by normalized absolute URL."""
    unique: dict[str, FileLink] = {}
    for link in links:
        normalized = link.url.split("#", 1)[0]
        unique.setdefault(normalized, link)
    return list(unique.values())


def is_exam_file(link: FileLink) -> bool:
    """Exclude site-wide attachments unrelated to exams."""
    filename = choose_filename(link)
    if parse_link_metadata(filename, link):
        return True
    if any(keyword in f"{filename} {link.label}" for keyword in NON_EXAM_FILE_KEYWORDS):
        return False
    context = f"{filename} {link.label} {link.page_title}"
    if any(keyword in context for keyword in NAVIGATION_KEYWORDS[:-1]):
        return True
    return bool(
        re.search(
            r"(?:10\d|11\d).{0,10}(?:第?[12][-_]?學期|[上下]學期|第[一二三四1234]次)",
            context,
        )
    )


def filter_exam_links(links: list[FileLink]) -> list[FileLink]:
    """Keep only links whose filename or page context indicates exam material."""
    return [link for link in deduplicate_links(links) if is_exam_file(link)]


def discover_static(
    session: requests.Session,
    start_url: str,
    strategy: str,
    logger: logging.Logger,
) -> list[FileLink]:
    """Crawl bounded static CMS pages and return direct file links."""
    queue = [(start_url, 0)]
    visited: set[str] = set()
    discovered: list[FileLink] = []

    while queue and len(visited) < MAX_CRAWL_PAGES:
        page_url, depth = queue.pop(0)
        normalized = page_url.split("#", 1)[0]
        if normalized in visited:
            continue
        visited.add(normalized)
        try:
            response = session.get(page_url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("  [page-error] %s: %s", page_url, exc)
            continue

        # 【debug】print first page status
        if depth == 0 and len(visited) == 1:
            pass  # 2026-07-30: removed debug log
        files, navigation = extract_links(response.text, response.url)
        discovered.extend(files)
        if depth >= MAX_CRAWL_DEPTH:
            continue
        for candidate_url, label in navigation:
            if should_follow(strategy, start_url, candidate_url, label):
                queue.append((candidate_url, depth + 1))

    logger.info("  crawled %d page(s)", len(visited))
    return filter_exam_links(discovered)


class PlaywrightFetcher:
    """Render JS pages while recycling Chromium to cap memory growth."""

    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger
        self.playwright: Any = None
        self.browser: Any = None
        self.pages_used = 0

    async def __aenter__(self) -> "PlaywrightFetcher":
        from playwright.async_api import async_playwright

        self.playwright = await async_playwright().start()
        await self._launch_browser()
        return self

    async def __aexit__(self, *_: object) -> None:
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    async def _launch_browser(self) -> None:
        if self.browser:
            await self.browser.close()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.pages_used = 0

    async def discover(self, url: str) -> list[FileLink]:
        """Render one source page and extract its file anchors."""
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        if self.pages_used >= PLAYWRIGHT_PAGES_PER_BROWSER:
            self.logger.info("  recycling Chromium after %d pages", self.pages_used)
            await self._launch_browser()

        page = await self.browser.new_page()
        self.pages_used += 1
        try:
            try:
                await page.goto(url, wait_until="networkidle", timeout=REQUEST_TIMEOUT * 1000)
            except PlaywrightTimeoutError:
                self.logger.warning("  network idle timed out; parsing the loaded DOM")
            try:
                await page.wait_for_selector(
                    "a[href*='.pdf'], a[href*='.doc'], a[href*='.zip']",
                    timeout=PARSER_TIMEOUT_MS,
                )
            except Exception:
                self.logger.info("  no file selector appeared within 10 seconds")
            files, _ = extract_links(await page.content(), page.url)
            return filter_exam_links(files)
        finally:
            await page.close()


def infer_level(school: dict[str, Any]) -> str:
    """Resolve StudyArk level from source category, then school name."""
    category_level = LEVEL_BY_CATEGORY.get(school.get("category", ""))
    if category_level:
        return category_level
    name = school.get("name", "")
    if "國小" in name or "國民小學" in name:
        return "國小"
    if "國中" in name or "國民中學" in name:
        return "國中"
    if any(word in name for word in ("高中", "高級中學", "高工", "高商")):
        return "高中"
    return "其他"


def parse_link_metadata(filename: str, link: FileLink) -> dict[str, Any] | None:
    """Parse the filename, then retry with anchor/page context."""
    for candidate in (
        filename,
        f"{link.label}{Path(filename).suffix}",
        f"{link.page_title}_{link.label}_{filename}",
    ):
        info = parse_filename(candidate)
        if info:
            return info
    return None


def target_for(
    school: dict[str, Any],
    filename: str,
    link: FileLink,
) -> Path:
    """Map one discovered file into the StudyArk directory structure."""
    county = school.get("county") or "其他X"
    school_name = school.get("name") or "未知學校"
    level = infer_level(school)
    info = parse_link_metadata(filename, link)
    has_answer = bool(re.search(r"答案|解答|解析|詳解", f"{filename}{link.label}"))

    if not info:
        filetype = "daan" if has_answer else "paper"
        return ARCHIVE_ROOT / county / level / "其他" / "其他" / filetype / filename

    grade = info["grade"]
    subject = info["subject"] or "其他"
    filetype = "daan" if info["has_answer"] or has_answer else "paper"
    parts = [
        county,
        f"{info['year']:03d}",
        f"第{info['term']}學期",
        info["exam_type"],
        school_name,
        grade,
        subject,
    ]
    if filetype == "daan":
        parts.append("解答")
    new_filename = safe_filename("_".join(parts)) + Path(filename).suffix.lower()
    return ARCHIVE_ROOT / county / level / grade / subject / filetype / new_filename


def validate_download(content: bytes, extension: str) -> bool:
    """Reject common HTML error pages returned as file downloads."""
    if extension == ".pdf":
        return content.startswith(b"%PDF")
    if extension in {".docx", ".xlsx", ".zip"}:
        return content.startswith(b"PK")
    return bool(content)


def extract_zip(
    content: bytes,
    school: dict[str, Any],
    source_link: FileLink,
    dry_run: bool,
    logger: logging.Logger,
) -> int:
    """Extract supported ZIP members directly into StudyArk targets."""
    written = 0
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        for member in archive.infolist():
            member_name = Path(member.filename).name
            extension = Path(member_name).suffix.lower()
            if member.is_dir() or extension not in ZIP_MEMBER_EXTENSIONS:
                continue
            filename = safe_filename(member_name)
            member_link = FileLink(source_link.url, filename, source_link.page_title)
            target = target_for(school, filename, member_link)
            if target.exists():
                logger.info("  [zip-skip] %s", target.relative_to(ARCHIVE_ROOT))
                continue
            logger.info("  [%s] %s", "dry-run" if dry_run else "zip", target.relative_to(ARCHIVE_ROOT))
            if dry_run:
                continue
            member_content = archive.read(member)
            if not validate_download(member_content, extension):
                logger.warning("  [zip-invalid] %s", member.filename)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(member_content)
            written += 1
    return written


def archive_links(
    session: requests.Session,
    school: dict[str, Any],
    links: list[FileLink],
    downloaded_urls: set[str],
    dry_run: bool,
    logger: logging.Logger,
) -> tuple[int, list[str], list[str]]:
    """Preview or download discovered links and return result details."""
    written = 0
    completed_urls: list[str] = []
    errors: list[str] = []
    # 【2026-07-30 fix】.edu.tw tad_uploader download 需 session cookie
    # 為避免每個 file 都 warmup 一次,先為遇的 cat_sn page 事先 batch warmup,
    # 这样 1 個 cat_sn 一個 page 即可,不是 N files = N warms
    cat_pages_visited: set[str] = set()
    def get_category_page(link: "FileLink") -> str | None:
        cat = parse_qs(urlparse(link.url).query).get("cat_sn")
        if not cat:
            return None
        base = urlparse(link.url)
        return (
            f"{base.scheme}://{base.netloc}{base.path}"
            f"?op=list_mode&list_mode=more&of_cat_sn={cat[0]}"
        )

    # Pre-warmup: visit each unique category page once
    for link in links:
        cp = get_category_page(link)
        if cp and cp not in cat_pages_visited:
            cat_pages_visited.add(cp)
            try:
                session.get(cp, timeout=REQUEST_TIMEOUT)
                time.sleep(0.3)  # gentle pacing between warmup calls
            except requests.RequestException:
                pass



    for link in links:
        if link.url in downloaded_urls:
            continue
        filename = choose_filename(link)
        target = target_for(school, filename, link)
        if target.exists():
            logger.info("  [exists] %s", target.relative_to(ARCHIVE_ROOT))
            completed_urls.append(link.url)
            continue
        if dry_run:
            logger.info("  [dry-run] %s", target.relative_to(ARCHIVE_ROOT))
            continue

        try:
            cat_page = get_category_page(link)
            # 【2026-07-30 bugfix】Referer 不可為中文 URL (requests 內部用 latin-1 encode header)
            # Use category page (ASCII only) as Referer — fallback to page_url
            referer = cat_page or link.url
            headers = {"Referer": referer}
            # 【2026-07-30】加 pacing,避免 IP 被 rate-limit 退绠
            time.sleep(0.5)
            response = session.get(link.url, timeout=REQUEST_TIMEOUT, headers=headers)
            response.raise_for_status()
            extension = Path(filename).suffix.lower()
            if not response.content:
                raise ValueError(f"empty response (content-type={response.headers.get('content-type', '?')})")
            if not validate_download(response.content, extension):
                raise ValueError(f"invalid {extension or 'file'} response (content-type={response.headers.get('content-type', '?')})")
            if extension == ".zip":
                written += extract_zip(response.content, school, link, False, logger)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(response.content)
                written += 1
                logger.info("  [downloaded] %s", target.relative_to(ARCHIVE_ROOT))
            completed_urls.append(link.url)
        except (OSError, ValueError, zipfile.BadZipFile, requests.RequestException) as exc:
            message = f"{link.url}: {exc}"
            errors.append(message)
            logger.warning("  [download-error] %s", message)
        time.sleep(0.5)
    return written, completed_urls, errors


async def run(args: argparse.Namespace, logger: logging.Logger) -> int:
    """Run the selected school batch."""
    schools = load_schools()
    state = load_state()
    school_state = state.setdefault("schools", {})

    if args.match:
        needle = args.match.casefold()
        schools = [school for school in schools if needle in school.get("name", "").casefold()]
    if not args.force and not args.dry_run:
        schools = [
            school
            for school in schools
            if school_state.get(school.get("name", ""), {}).get("status") != "completed"
        ]
    if args.limit is not None:
        schools = schools[: args.limit]

    logger.info("Selected %d school(s) from %s", len(schools), SOURCE_FILE)
    if args.dry_run:
        logger.info("DRY RUN — files and state will not be changed")
    if not schools:
        return 0

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    needs_browser = any(strategy_for(school["url"]) == "playwright" for school in schools)
    browser_context = PlaywrightFetcher(logger) if needs_browser else None

    async def process(playwright_fetcher: PlaywrightFetcher | None) -> None:
        for index, school in enumerate(schools, 1):
            name = school.get("name", "未知學校")
            url = school.get("url", "")
            strategy = strategy_for(url)
            logger.info("[%d/%d] %s [%s] %s", index, len(schools), name, strategy, url)
            previous = school_state.get(name, {})
            downloaded_urls = set(previous.get("downloaded_urls", []))

            try:
                if strategy == "playwright":
                    if not playwright_fetcher:
                        raise RuntimeError("Playwright is not available")
                    links = await playwright_fetcher.discover(url)
                else:
                    links = discover_static(session, url, strategy, logger)
                logger.info("  discovered %d unique file(s)", len(links))
                written, completed_urls, errors = archive_links(
                    session,
                    school,
                    links,
                    downloaded_urls,
                    args.dry_run,
                    logger,
                )
                logger.info("  archived %d new file(s)", written)

                if not args.dry_run:
                    downloaded_urls.update(completed_urls)
                    school_state[name] = {
                        "status": "completed" if not errors else "partial",
                        "url": url,
                        "strategy": strategy,
                        "last_run": datetime.now(timezone.utc).isoformat(),
                        "discovered": len(links),
                        "downloaded_urls": sorted(downloaded_urls),
                        "errors": errors[-20:],
                    }
                    save_state(state)
            except Exception as exc:
                logger.exception("  [school-error] %s", exc)
                if not args.dry_run:
                    school_state[name] = {
                        **previous,
                        "status": "failed",
                        "url": url,
                        "strategy": strategy,
                        "last_run": datetime.now(timezone.utc).isoformat(),
                        "error": str(exc),
                    }
                    save_state(state)

            if index < len(schools):
                await asyncio.sleep(2.0)

    if browser_context:
        async with browser_context as playwright_fetcher:
            await process(playwright_fetcher)
    else:
        await process(None)
    return 0


def parse_args() -> argparse.Namespace:
    """Parse command-line options."""
    parser = argparse.ArgumentParser(description="Archive .edu.tw school exam files")
    parser.add_argument("--dry-run", action="store_true", help="Discover and display targets only")
    parser.add_argument("--limit", type=int, default=None, help="Maximum schools to process")
    parser.add_argument("--match", help="Process school names containing this text")
    parser.add_argument("--force", action="store_true", help="Re-scan schools marked completed")
    return parser.parse_args()


def main() -> int:
    """CLI entry point."""
    args = parse_args()
    if args.limit is not None and args.limit < 1:
        raise SystemExit("--limit must be at least 1")
    logger = setup_logging()
    return asyncio.run(run(args, logger))


if __name__ == "__main__":
    sys.exit(main())
