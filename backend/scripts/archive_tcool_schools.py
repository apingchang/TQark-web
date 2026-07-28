#!/usr/bin/env python3
"""
tcool.cc 學校考古題 archive (2026-07-27 新增, 2026-07-28 改)

任務: 從 external_sources.json 抓 .edu.tw 高雄 affairs.kh.edu.tw 學校考卷
      直接存成 StudyArk 結構:
        /mnt/my_book/考題收集/<county>/<level>/<grade>/<subject>/<paper|daan>/<file>

Pipeline:
1. 讀 external_sources.json,filter link_type in ['school_web', 'nas']
2. 對每個學校:
   - 用 Playwright 進 page (JS-rendered)
   - 列學期 (e.g., "114學年度第2學期補考題庫")
   - 進每學期 page
   - 列所有 .pdf/.doc/.docx 下載連結
   - Parse 檔名 → year/term/grade/subject/exam_type/has_answer
   - 決定 school level (國中/高中/國小) 從 external_sources.json 拿
   - 存到 <county>/<level>/<grade>/<subject>/<paper|daan>/file.{pdf,docx,doc}

用法:
  uv run python scripts/archive_tcool_schools.py [--school-id 5365] [--dry-run]

目標 archive 路徑 (跟 StudyArk 一致):
  /mnt/my_book/考題收集/<county>/國中/一年級/英語/paper/高雄市_111_第1學期_補考_高雄市五福國中_一年級_英語.pdf
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

# 讓 import 找得到 app 模組
BACKEND_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BACKEND_DIR))

# Import migrate script 的 parser (2026-07-28 共用)
sys.path.insert(0, str(Path(__file__).parent))
from migrate_tcool_to_studyark_structure import parse_filename, load_school_levels, ARCHIVE_ROOT as _MIGRATE_ARCHIVE_ROOT

# 確保 ARCHIVE_ROOT 跟 migrate script 一致
ARCHIVE_ROOT = _MIGRATE_ARCHIVE_ROOT

# ============================================================
# 設定
# ============================================================
STATE_FILE = ARCHIVE_ROOT / "logs" / "tcool_schools_status.json"
LOG_FILE = ARCHIVE_ROOT / "logs" / "tcool_schools.log"

# 一次跑的學校數上限 (避免太長)
DEFAULT_BATCH = 1

# Load school levels from external_sources.json (2026-07-28)
SCHOOL_LEVELS = load_school_levels()


def setup_logging():
    ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    import logging
    log = logging.getLogger("tcool_archive")
    log.setLevel(logging.INFO)
    if log.handlers:
        return log
    fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    log.addHandler(ch)
    return log


def load_sources():
    """讀 external_sources.json"""
    sources_file = BACKEND_DIR / "data" / "external_sources.json"
    if not sources_file.exists():
        return None, "external_sources.json 不存在"
    return json.loads(sources_file.read_text(encoding="utf-8")), None


def filter_schools(sources, link_types=None):
    """Filter schools by link_type + 限制 schools"""
    schools = sources.get("schools", [])
    if link_types:
        schools = [s for s in schools if s.get("link_type") in link_types]
    return schools


def safe_filename(name):
    """清理檔名"""
    # Windows 不允許的字元
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    return name.strip()


def extract_zip_to_studyark_structure(zip_path: Path, county: str, school_name: str, log) -> int:
    """
    【2026-07-28 新】解壓 .zip 到 StudyArk 結構。
    假設 .zip 內檔名也是 <year>_<term>_<exam>_<school>_<grade>_<subject>.<ext> 之類。
    解壓後,對每個檔案重新跑 parse_filename → 放到正確 folder。
    解壓成功後刪除 .zip 本身。
    Returns 解壓出的檔案數量。
    """
    import zipfile
    if not zipfile.is_zipfile(zip_path):
        log.warning(f"  [not-a-zip] {zip_path.name}")
        return 0

    extracted_count = 0
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            for member in zf.namelist():
                # Skip 目錄 or hidden
                if member.endswith('/') or member.startswith('.'):
                    continue
                inner_fname = Path(member).name
                if not inner_fname:
                    continue
                inner_ext = Path(inner_fname).suffix.lower()
                if inner_ext not in ('.pdf', '.docx', '.doc', '.xlsx', '.xls'):
                    continue

                # Read content
                with zf.open(member) as src:
                    content = src.read()

                # Validate PDF magic bytes
                if inner_fname.lower().endswith('.pdf') and not content.startswith(b'%PDF'):
                    log.warning(f"  [skip-inner-not-PDF] {inner_fname}")
                    continue

                # Parse inner filename → target path
                info = parse_filename(inner_fname)
                if not info:
                    # Fallback: 解壓到 county/school/ 原始位置
                    log.warning(f"  [inner-parse-fail] {inner_fname} → 保留到 {county}/{school_name}/")
                    fallback_dir = ARCHIVE_ROOT / county / school_name
                    fallback_dir.mkdir(parents=True, exist_ok=True)
                    target = fallback_dir / safe_filename(inner_fname)
                else:
                    level = SCHOOL_LEVELS.get(school_name, "其他")
                    if level == "其他":
                        grade = info["grade"]
                        if grade in ("四年級", "五年級", "六年級"):
                            level = "國小"
                        elif grade in ("七年級", "八年級", "九年級"):
                            level = "國中"
                        elif grade in ("一年級", "二年級", "三年級"):
                            level = "高中"
                        else:
                            level = "其他"

                    if level == "其他":
                        log.warning(f"  [inner-level-unknown] {inner_fname}, 保留到 {county}/{school_name}/")
                        fallback_dir = ARCHIVE_ROOT / county / school_name
                        fallback_dir.mkdir(parents=True, exist_ok=True)
                        target = fallback_dir / safe_filename(inner_fname)
                    else:
                        grade = info["grade"]
                        subject = info["subject"] or "其他"
                        filetype = "daan" if info["has_answer"] else "paper"

                        new_name_parts = [
                            county,
                            f"{info['year']:03d}",
                            f"第{info['term']}學期",
                            info['exam_type'],
                            school_name,
                            grade,
                            subject,
                        ]
                        if info['has_answer']:
                            new_name_parts.append("解答")
                        ext = Path(inner_fname).suffix
                        new_name = "_".join(new_name_parts) + ext
                        target = ARCHIVE_ROOT / county / level / grade / subject / filetype / new_name

                # Write (skip if exists)
                if target.exists():
                    log.info(f"  [zip-skip] {target.name} (already exists)")
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                extracted_count += 1
                log.info(f"  [zip-extracted] {target.relative_to(ARCHIVE_ROOT)} ({len(content)} bytes)")

        # 刪除 zip 本身
        zip_path.unlink()
        log.info(f"  [zip-removed] {zip_path.name}")
        return extracted_count

    except zipfile.BadZipFile as e:
        log.warning(f"  [bad-zip] {zip_path.name}: {e}")
        return 0
    except Exception as e:
        log.warning(f"  [zip-error] {zip_path.name}: {e}")
        return 0


async def archive_school(browser, school, dry_run=False):
    """對單一學校 archive"""
    from playwright.async_api import async_playwright

    name = school.get("name", "?")
    county = school.get("county") or "其他X"
    url = school.get("url", "")
    link_type = school.get("link_type", "")

    log.info(f"[{name}] ({link_type}) start: {url}")

    # Read state
    state = {}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    state.setdefault(name, {"downloaded": [], "failed": []})
    already_downloaded = set(state[name]["downloaded"])

    page = await browser.new_page()

    try:
        if "affairs.kh.edu.tw" in url:
            count = await archive_kh_school(page, name, county, None, url, already_downloaded, dry_run)
        elif "edu.tw" in url and "drive.google.com" not in url:
            count = await archive_edu_school(page, name, county, url, already_downloaded, dry_run)
        else:
            log.warning(f"[{name}] unsupported URL type: {url}")
            return 0

        state[name].update({
            "last_run": time.strftime("%Y-%m-%d %H:%M:%S"),
            "county": county,
            "link_type": link_type,
            "url": url,
        })

        # Save state
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))
        log.info(f"[{name}] done: {count} new downloaded")
        return count

    except Exception as e:
        log.error(f"[{name}] ERROR: {e}")
        state[name]["error"] = str(e)
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))
        return 0
    finally:
        await page.close()


async def archive_kh_school(page, school_name, county, school_url_level, url, already_downloaded, dry_run):
    """
    高雄 affairs.kh.edu.tw 學校
    URL: https://affairs.kh.edu.tw/{school_id}/upload/upload_list/{page}
    學期是 sub-link: /upload/file_list/{file_id}
    File list 頁有 .pdf/.doc/.docx 直接下載

    【2026-07-28 改】直接寫到 StudyArk 結構:
      <county>/<level>/<grade>/<subject>/<filetype>/file
    """
    log = setup_logging()

    await page.goto(url, timeout=30000)
    await page.wait_for_timeout(5000)  # JS render

    # 找學期 file_list 連結 (capture name + URL)
    sem_links = await page.locator('a').all()
    file_list_pages = []  # (sem_name, sem_url)
    for link in sem_links:
        href = (await link.get_attribute("href")) or ""
        text = (await link.text_content() or '').strip()
        if "/upload/file_list/" in href and text:
            file_list_pages.append((text, href))

    log.info(f"[{school_name}] found {len(file_list_pages)} semester pages")

    count = 0
    skipped_parse_fail = 0
    for sem_name, sem_url in file_list_pages:
        await page.goto(sem_url, timeout=30000)
        await page.wait_for_timeout(5000)

        # Get title from page (e.g. "114學年度第2學期補考題庫")
        title = await page.title()
        # Try to extract 學年度 pattern from the link text or title
        sem_match = re.search(r'(\d{3}學年度[上下中第\d]+學期[上中下]?期?\S{0,10}?(?:補考|段考)?題庫)', sem_name + title)
        if sem_match:
            sem_dir_name = sem_match.group(1)
        else:
            sem_dir_name = sem_name  # fallback to link text

        # Get file list
        all_links = await page.locator('a').all()
        file_links = []
        for link in all_links:
            href = (await link.get_attribute("href")) or ""
            text = (await link.text_content() or '').strip()
            # Match direct download links (not "線上開啟" google viewer)
            if re.search(r"\.(pdf|docx|doc|xlsx|xls)$", href, re.I) and "google.com/viewer" not in href:
                file_links.append((text, href))

        log.info(f"[{school_name}/{sem_dir_name[:30]}] {len(file_links)} files")

        for fname_orig, file_url in file_links:
            fname_safe = safe_filename(fname_orig)
            if fname_safe in already_downloaded:
                continue

            # Parse filename for structure
            info = parse_filename(fname_safe)
            if not info:
                skipped_parse_fail += 1
                log.warning(f"  [parse-fail] {fname_safe}, falling back to sem_dir")
                # Fallback: keep old structure for unparseable
                sem_dir = (school_url_level if school_url_level else (ARCHIVE_ROOT / county / school_name)) / safe_filename(sem_dir_name)
                sem_dir.mkdir(parents=True, exist_ok=True)
                target = sem_dir / fname_safe
            else:
                # Determine level: prefer external_sources.json match, else from grade
                level = SCHOOL_LEVELS.get(school_name, "其他")
                if level == "其他":
                    grade = info["grade"]
                    if grade in ("四年級", "五年級", "六年級"):
                        level = "國小"
                    elif grade in ("七年級", "八年級", "九年級"):
                        level = "國中"
                    elif grade in ("一年級", "二年級", "三年級"):
                        level = "高中"
                    else:
                        level = "其他"

                if level == "其他":
                    skipped_parse_fail += 1
                    log.warning(f"  [level-unknown] {fname_safe}, skipping")
                    continue

                grade = info["grade"]
                subject = info["subject"] or "其他"
                filetype = "daan" if info["has_answer"] else "paper"

                # Build new filename: <county>_<year>_<term>_<exam-type>_<school>_<grade>_<subject>[_解答].<ext>
                new_name_parts = [
                    county,
                    f"{info['year']:03d}",
                    f"第{info['term']}學期",
                    info['exam_type'],
                    school_name,
                    grade,
                    subject,
                ]
                if info['has_answer']:
                    new_name_parts.append("解答")
                ext = Path(fname_safe).suffix
                new_name = "_".join(new_name_parts) + ext

                target = ARCHIVE_ROOT / county / level / grade / subject / filetype / new_name

            if target.exists():
                log.info(f"  [skip] {target.name} (already exists)")
                continue

            if dry_run:
                log.info(f"  [dry-run] {target.relative_to(ARCHIVE_ROOT)}")
                continue

            try:
                # Direct download via httpx (no need for browser context)
                import httpx
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.get(file_url, follow_redirects=True)
                    if resp.status_code == 200:
                        content = resp.content
                        # Validate PDF magic bytes if PDF
                        if fname_safe.lower().endswith('.pdf') and not content.startswith(b'%PDF'):
                            log.warning(f"  [skip] {fname_safe} not a valid PDF ({len(content)} bytes)")
                            continue
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(content)
                        log.info(f"  [✓] {target.relative_to(ARCHIVE_ROOT)} ({len(content)} bytes)")
                        count += 1

                        # 【2026-07-28】如果是 .zip, 解壓到 StudyArk 結構后刪除 zip
                        if fname_safe.lower().endswith('.zip'):
                            log.info(f"  [zip-detected] {fname_safe}, 開始解壓...")
                            extracted = extract_zip_to_studyark_structure(target, county, school_name, log)
                            count += extracted - 1  # zip 本身不算 1 file, 被解壓的才是
                    else:
                        log.warning(f"  [fail {resp.status_code}] {fname_safe}")
            except Exception as e:
                log.warning(f"  [error] {fname_safe}: {e}")

            # Politeness delay
            await asyncio.sleep(1.0)

    if skipped_parse_fail:
        log.warning(f"[{school_name}] skipped {skipped_parse_fail} files (parse-fail or level-unknown)")
    return count


async def archive_edu_school(page, school_name, county, url, already_downloaded, dry_run):
    """通用 .edu.tw 學校 — 用 Playwright 抓所有 PDF 連結

    【2026-07-28 改】直接寫到 StudyArk 結構:
      <county>/<level>/<grade>/<subject>/<filetype>/file
    """
    log = setup_logging()

    await page.goto(url, timeout=30000)
    await page.wait_for_timeout(8000)

    body = await page.content()
    file_links = re.findall(r'href="([^"]*\.(?:pdf|docx|doc|zip|xlsx|xls))(?:\?[^"]*)?"', body, re.I)

    # Dedupe + filter google viewer
    file_links = [l for l in file_links if "google.com" not in l]
    file_links = sorted(set(file_links))

    log.info(f"[{school_name}] {len(file_links)} files in HTML")

    count = 0
    skipped_parse_fail = 0
    import httpx
    async with httpx.AsyncClient(timeout=30) as client:
        for file_url in file_links:
            full_url = urljoin(url, file_url)
            fname = Path(file_url).name
            fname_safe = safe_filename(fname)

            if fname_safe in already_downloaded:
                continue

            # Parse filename for structure
            info = parse_filename(fname_safe)
            if not info:
                skipped_parse_fail += 1
                # Fallback to unsorted under school dir
                year_match = re.search(r'(\d{3})學年度', fname)
                year_dir = ARCHIVE_ROOT / county / school_name / (year_match.group(0) if year_match else "unsorted")
                year_dir.mkdir(parents=True, exist_ok=True)
                target = year_dir / fname_safe
            else:
                # Determine level
                level = SCHOOL_LEVELS.get(school_name, "其他")
                if level == "其他":
                    grade = info["grade"]
                    if grade in ("四年級", "五年級", "六年級"):
                        level = "國小"
                    elif grade in ("七年級", "八年級", "九年級"):
                        level = "國中"
                    elif grade in ("一年級", "二年級", "三年級"):
                        level = "高中"
                    else:
                        level = "其他"

                if level == "其他":
                    skipped_parse_fail += 1
                    log.warning(f"  [level-unknown] {fname_safe}, skipping")
                    continue

                grade = info["grade"]
                subject = info["subject"] or "其他"
                filetype = "daan" if info["has_answer"] else "paper"

                new_name_parts = [
                    county,
                    f"{info['year']:03d}",
                    f"第{info['term']}學期",
                    info['exam_type'],
                    school_name,
                    grade,
                    subject,
                ]
                if info['has_answer']:
                    new_name_parts.append("解答")
                ext = Path(fname_safe).suffix
                new_name = "_".join(new_name_parts) + ext

                target = ARCHIVE_ROOT / county / level / grade / subject / filetype / new_name

            if target.exists():
                continue

            if dry_run:
                log.info(f"  [dry-run] {target.relative_to(ARCHIVE_ROOT)}")
                continue

            try:
                resp = await client.get(full_url, follow_redirects=True)
                if resp.status_code == 200:
                    if fname_safe.lower().endswith('.pdf') and not resp.content.startswith(b'%PDF'):
                        log.warning(f"  [skip-not-PDF] {fname_safe}")
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(resp.content)
                    log.info(f"  [✓] {target.relative_to(ARCHIVE_ROOT)} ({len(resp.content)} bytes)")
                    count += 1

                    # 【2026-07-28】如果是 .zip, 解壓到 StudyArk 結構后刪除 zip
                    if fname_safe.lower().endswith('.zip'):
                        log.info(f"  [zip-detected] {fname_safe}, 開始解壓...")
                        extracted = extract_zip_to_studyark_structure(target, county, school_name, log)
                        count += extracted - 1  # zip 本身不算,被解壓的才是
            except Exception as e:
                log.warning(f"  [error] {fname_safe}: {e}")

            await asyncio.sleep(1.0)

    if skipped_parse_fail:
        log.warning(f"[{school_name}] skipped {skipped_parse_fail} files (parse-fail or level-unknown)")
    return count


async def main_async(args):
    from playwright.async_api import async_playwright

    sources, err = load_sources()
    if err:
        log.error(err)
        return 1

    schools = filter_schools(sources, link_types=['school_web', 'nas'])
    # Filter by school name or county substring if provided
    if args.match:
        schools = [s for s in schools if args.match.lower() in s.get('name', '').lower()]

    if args.school_id:
        # Filter by URL containing school_id
        schools = [s for s in schools if args.school_id in s.get('url', '')]

    if args.host:
        schools = [s for s in schools if args.host in s.get('url', '')]

    schools = schools[:args.batch]

    log.info(f"Archive target: {len(schools)} schools")
    for s in schools:
        log.info(f"  - {s.get('name', '?')[:30]:30s} [{s.get('link_type')}] {s.get('url', '')[:60]}")

    if args.dry_run:
        log.info("DRY RUN — no actual downloads")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        for school in schools:
            await archive_school(browser, school, dry_run=args.dry_run)
            await asyncio.sleep(2.0)

        await browser.close()

    log.info("All done")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Archive tcool.cc schools PDFs")
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH, help="Number of schools to process")
    parser.add_argument("--match", default=None, help="Substring match school name (e.g., '板橋', '五福')")
    parser.add_argument("--school-id", default=None, help="Filter by URL school id (e.g., 5365)")
    parser.add_argument("--host", default=None, help="Filter by URL host substring (e.g., 'affairs.kh.edu.tw')")
    parser.add_argument("--dry-run", action="store_true", help="List only, don't download")
    args = parser.parse_args()

    global log
    log = setup_logging()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main() or 0)
