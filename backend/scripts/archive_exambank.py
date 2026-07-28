#!/usr/bin/env python3
"""
ExamBank (exambank.darrenlu.com) 全國考卷 archive (2026-07-28 新增)

任務: 從 ExamBank sitemap 抓 8521 papers,
      用 Playwright 點 download button 拿 R2 PDF URL,
      存成 StudyArk 結構:
        /mnt/my_book/考題收集/<county>/<level>/<grade>/<subject>/<paper|daan>/<file>

Pipeline:
1. 抓 sitemap (https://exambank.darrenlu.com/sitemap.xml)
2. 解析每個 paper URL → year, school, grade, subject, term (1/2/3 段考)
3. School → county mapping (hardcoded table, 從學校位置推縣市)
4. 對每個 paper:
   - 用 Playwright 點 「下載試卷 PDF」 button
   - 攔截 download event → 拿 R2 URL (signed, 60 秒有效)
   - 用 httpx GET 拿到 R2 PDF
   - 寫到 StudyArk 結構

用法:
  uv run python scripts/archive_exambank.py [--batch 100] [--dry-run] [--resume]

目標 archive 路徑 (跟 StudyArk 一致):
  /mnt/my_book/考題收集/高雄市/高中/高一年級/觀光餐旅業導論/paper/高雄市_109_第1段考_國立鳳山商工_高一年級_觀光餐旅業導論.pdf

【2026-07-28】初始版本 — 全 sitemap 8521 papers 預估 ~7 小時
"""

import argparse
import asyncio
import json
import logging
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

BACKEND_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(Path(__file__).parent))

# Reuse tcool/migrate 設定 + parser
from migrate_tcool_to_studyark_structure import ARCHIVE_ROOT as _MIGRATE_ARCHIVE_ROOT

ARCHIVE_ROOT = _MIGRATE_ARCHIVE_ROOT
STATE_FILE = ARCHIVE_ROOT / "logs" / "exambank_status.json"
LOG_FILE = ARCHIVE_ROOT / "logs" / "exambank.log"
SITEMAP_URL = "https://exambank.darrenlu.com/sitemap.xml"

# 一次跑的 papers 數上限 (避免 timeout)
DEFAULT_BATCH = 100


# ============================================================
# School → County 對照 (從 sitemap 觀察 + 一般常識)
# 不在表內的會 fallback 到 「未分類」並寫到 logs
# ============================================================
SCHOOL_TO_COUNTY = {
    # === 高雄市 ===
    "國立鳳山商工": "高雄市",
    "國立鳳山高中": "高雄市",
    "市立三民高中": "高雄市",
    "市立六龜高中": "高雄市",
    "市立林園高中附設國中": "高雄市",
    "市立林園高中": "高雄市",
    "市立新興國中": "高雄市",
    "市立三民國中": "高雄市",
    "市立民族國中": "高雄市",
    "市立茂林國中": "高雄市",
    "市立大樹國中": "高雄市",
    "市立鼓山高中": "高雄市",
    "市立左營國中": "高雄市",
    "市立高雄女中": "高雄市",
    "市立高雄中學": "高雄市",
    "市立中山高中": "高雄市",
    "市立中山高中附設國中": "高雄市",

    # === 臺北市 ===
    "臺北市立建國高級中學": "臺北市",
    "市立建國高級中學": "臺北市",
    "市立松山家商": "臺北市",
    "市立永春高中": "臺北市",  # 應該是新北市但 同名
    "市立麗山高中": "臺北市",
    "市立中山國中": "臺北市",
    "市立景美國中": "臺北市",
    "市立民生國中": "臺北市",
    "市立濱江國中": "臺北市",
    "市立敦化國中": "臺北市",
    "市立介壽國中": "臺北市",
    "市立蘭雅國中": "臺北市",
    "市立天母國中": "臺北市",
    "市立石牌國中": "臺北市",
    "市立北投國中": "臺北市",
    "市立士林國中": "臺北市",
    "市立金華國中": "臺北市",
    "市立龍山國中": "臺北市",
    "市立萬芳高中": "臺北市",
    "市立百齡高中": "臺北市",
    "市立明湖國中": "臺北市",
    "市立內湖國中": "臺北市",
    "市立碧湖國中": "臺北市",
    "市立大直高中": "臺北市",
    "市立成功高中": "臺北市",
    "市立第一女子高級中學": "臺北市",
    "市立中山女子高級中學": "臺北市",

    # === 新北市 ===
    "市立石門國中": "新北市",
    "市立崇林國中": "新北市",
    "市立八里國中": "新北市",
    "市立福豐國中": "新北市",
    "市立中和高中": "新北市",
    "市立板橋高中": "新北市",
    "市立新莊高中": "新北市",
    "市立新北高中": "新北市",
    "市立三重高中": "新北市",
    "私立黎明高中": "新北市",
    "私立黎明高中附設國中": "新北市",

    # === 桃園市 ===
    "市立同德國中": "桃園市",
    "市立大溪國中": "桃園市",
    "市立內壢國中": "桃園市",
    "市立桃園高中": "桃園市",
    "市立武陵高中": "桃園市",

    # === 臺中市 ===
    "市立中港高中附設國中": "臺中市",
    "市立臺中一中": "臺中市",
    "市立臺中女中": "臺中市",
    "市立臺中二中": "臺中市",
    "市立文華高中": "臺中市",

    # === 臺南市 ===
    "市立大成國中": "臺南市",
    "市立歸仁國中": "臺南市",
    "市立臺南一中": "臺南市",
    "市立臺南女中": "臺南市",

    # === 新竹 ===
    "市立五峰國中": "新竹縣",
    "市立中興國中": "新竹市",
    "市立北興國中": "新竹市",
    "市立文昌國中": "新竹市",
    "市立新竹高中": "新竹市",
    "市立新竹女中": "新竹市",
    "國立新竹高中": "新竹市",
    "國立新竹女中": "新竹市",

    # === 苗栗 ===
    "縣立大同國中": "苗栗縣",  # 預設苗栗 (有同名多縣市)
    "市立苗栗高中": "苗栗縣",

    # === 彰化縣 ===
    "縣立福興國中": "彰化縣",
    "縣立溪湖國中": "彰化縣",
    "縣立埔心國中": "彰化縣",
    "縣立埤頭國中": "彰化縣",
    "縣立二水國中": "彰化縣",
    "縣立彰化高中": "彰化縣",
    "縣立彰化女中": "彰化縣",
    "國立員林家商": "彰化縣",
    "國立員林高中": "彰化縣",

    # === 屏東縣 ===
    "市立大灣國中": "屏東縣",
    "縣立屏東高中": "屏東縣",
    "縣立屏東女中": "屏東縣",

    # === 雲林 ===
    "縣立斗六高中": "雲林縣",

    # === 嘉義 ===
    "縣立嘉義高中": "嘉義市",
    "市立嘉義女中": "嘉義市",

    # === 宜蘭 ===
    "縣立宜蘭高中": "宜蘭縣",
    "縣立蘭陽女中": "宜蘭縣",

    # === 花蓮 ===
    "縣立花蓮高中": "花蓮縣",
    "縣立花蓮女中": "花蓮縣",
    "國立花蓮高工": "花蓮縣",

    # === 臺東 ===
    "縣立臺東高中": "臺東縣",
    "縣立臺東女中": "臺東縣",

    # === 基隆 ===
    "市立基隆高中": "基隆市",
    "市立基隆女中": "基隆市",

    # === 其他常見 ===
    "市立平南國中": "彰化縣",
}


# ============================================================
# Logging
# ============================================================
def setup_logging():
    ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("exambank_archive")
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


# ============================================================
# State file management (resumable)
# ============================================================
def load_state() -> dict:
    """Load state: { paper_url: {school, county, status, downloaded_at} }"""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


# ============================================================
# Sitemap + URL parsing
# ============================================================
def fetch_sitemap(log) -> list[str]:
    """從 sitemap.xml 抓所有 paper URLs"""
    import httpx
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.get(SITEMAP_URL, follow_redirects=True)
            resp.raise_for_status()
        xml = resp.text
    except Exception as e:
        log.error(f"Failed to fetch sitemap: {e}")
        return []

    urls = re.findall(r'<loc>(https?://exambank\.darrenlu\.com/paper/[^<]+)</loc>', xml)
    log.info(f"  sitemap: {len(urls)} paper URLs")
    return urls


def parse_paper_url(url: str) -> dict | None:
    """
    Parse paper URL → metadata
    URL format: https://exambank.darrenlu.com/paper/{year}-{title}-{8-char-hash}
    Title format:
      - Senior: 109年{學校}高一{科目}第N次段考
      - Junior: 111年{學校}七年級{科目}第N次段考

    Returns dict with: year, school, grade, subject, exam_term, level
    Returns None if can't parse.
    """
    m = re.match(r'https?://exambank\.darrenlu\.com/paper/(\d+)-(.+)-([a-f0-9]{8})$', url)
    if not m:
        return None
    year, title, hash_id = m.groups()

    # Try senior pattern: 學校 + 高X + 科目 + 第N
    m_senior = re.search(r'^\d+年(.+?)(高[一二三])([^第]+)第', title)
    if m_senior:
        school, grade_short, subject = m_senior.groups()
        grade = grade_short + "年級"
        exam_term_match = re.search(r'第(\d+)(?:次)?', title)
        exam_term = exam_term_match.group(1) if exam_term_match else "?"
        # Term: 段考序 (1, 2, 3 段考 → 第1/2/3 學期概念上類似)
        return {
            "year": int(year),
            "school": school.strip(),
            "grade": grade,
            "subject": subject.strip(),
            "exam_term": exam_term,
            "level": "senior",
            "title": title,
            "hash": hash_id,
        }

    # Try junior pattern: 學校 + X年級 + 科目 + 第N
    m_junior = re.search(r'^\d+年(.+?)([七八九])年級([^第]+)第', title)
    if m_junior:
        school, grade_num, subject = m_junior.groups()
        grade = grade_num + "年級"
        exam_term_match = re.search(r'第(\d+)(?:次)?', title)
        exam_term = exam_term_match.group(1) if exam_term_match else "?"
        return {
            "year": int(year),
            "school": school.strip(),
            "grade": grade,
            "subject": subject.strip(),
            "exam_term": exam_term,
            "level": "junior",
            "title": title,
            "hash": hash_id,
        }

    return None


def guess_county(school: str) -> str | None:
    """從學校名猜 county (用 SCHOOL_TO_COUNTY table)"""
    # Direct match
    if school in SCHOOL_TO_COUNTY:
        return SCHOOL_TO_COUNTY[school]

    # Strip prefix patterns
    clean = re.sub(r'^(國立|市立|縣立|私立|臺北市立|臺中市立|高雄市立)', '', school)
    if clean in SCHOOL_TO_COUNTY:
        return SCHOOL_TO_COUNTY[clean]

    # 【2026-07-28 新】联合考 title edge case
    # 例: "高雄市七年級數學..." — 高雄市聯合段考
    if school in ('高雄市', '臺北市', '新北市', '桃園市', '臺中市', '臺南市',
                  '新竹市', '新竹縣', '苗栗縣', '彰化縣', '南投縣', '雲林縣',
                  '嘉義市', '嘉義縣', '屏東縣', '宜蘭縣', '花蓮縣', '臺東縣',
                  '澎湖縣', '金門縣', '連江縣', '基隆市'):
        return school  # 本身就是 county

    # Prefix-based heuristic
    if school.startswith('臺北市立'):
        return '臺北市'
    if school.startswith('新北市立'):
        return '新北市'
    if school.startswith('桃園市立'):
        return '桃園市'
    if school.startswith('臺中市立'):
        return '臺中市'
    if school.startswith('臺南市立'):
        return '臺南市'
    if school.startswith('高雄市立'):
        return '高雄市'
    if school.startswith('市立'):
        return '未分類'  # 不夠 specific
    if school.startswith('縣立'):
        return '未分類'

    return None


# ============================================================
# Playwright: 拿 R2 download URL
# ============================================================
async def get_r2_url(page, paper_url: str, log) -> str | None:
    """
    用 Playwright 點下載按鈕,攔截 download event 拿 R2 URL.
    Returns R2 URL (signed, 60s valid) or None.
    """
    try:
        await page.goto(paper_url, timeout=30000)
        await page.wait_for_timeout(2000)  # Let page render + JS init

        # 點下載按鈕, 攔截 download event
        async with page.expect_download(timeout=15000) as dl_info:
            btn = await page.query_selector('button:has-text("下載"), a:has-text("下載")')
            if btn:
                await btn.click()
            else:
                log.warning(f"  [no-button] {paper_url}")
                return None

        dl = await dl_info.value
        return dl.url
    except Exception as e:
        log.warning(f"  [playwright-error] {paper_url}: {str(e)[:100]}")
        return None


# ============================================================
# Main pipeline
# ============================================================
async def archive_paper(
    page,
    paper_url: str,
    state: dict,
    log,
    dry_run: bool = False,
) -> bool:
    """處理單個 paper"""
    # 跳過已 done
    if state.get(paper_url, {}).get("status") == "done":
        return False

    # 解析 URL
    info = parse_paper_url(paper_url)
    if not info:
        log.warning(f"  [parse-fail] {paper_url}")
        state[paper_url] = {"status": "parse_fail"}
        return False

    # 對應 county
    county = guess_county(info["school"])
    if not county:
        log.warning(f"  [no-county] school='{info['school']}' → 未分類")
        state[paper_url] = {"status": "no_county", "info": info}
        return False

    if dry_run:
        log.info(f"  [dry-run] {county}/{info['level']}/{info['grade']}/{info['subject']}/")
        return False

    # 拿 R2 URL
    r2_url = await get_r2_url(page, paper_url, log)
    if not r2_url:
        state[paper_url] = {"status": "fetch_fail", "info": info, "county": county}
        return False

    # 下載 PDF
    import httpx
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(r2_url)
            if resp.status_code != 200:
                log.warning(f"  [http {resp.status_code}] {paper_url}")
                state[paper_url] = {"status": "http_fail", "info": info, "county": county}
                return False

            content = resp.content
            if not content.startswith(b'%PDF'):
                log.warning(f"  [not-PDF] {paper_url} ({len(content)} bytes)")
                state[paper_url] = {"status": "not_pdf", "info": info, "county": county}
                return False

            # 決定 target path
            level_zh = "高中" if info["level"] == "senior" else "國中"
            target_dir = ARCHIVE_ROOT / county / level_zh / info["grade"] / info["subject"] / "paper"
            target_dir.mkdir(parents=True, exist_ok=True)

            # New filename: {county}_{year:03d}_第{exam}段考_{school}_{grade}_{subject}.pdf
            # Use original suggested filename from R2 response if available, else construct
            fname_parts = [
                county,
                f"{info['year']:03d}",
                f"第{info['exam_term']}段考",
                info["school"],
                info["grade"],
                info["subject"],
            ]
            new_name = "_".join(fname_parts) + ".pdf"
            target = target_dir / new_name

            if target.exists():
                log.info(f"  [skip] {target.relative_to(ARCHIVE_ROOT)}")
                state[paper_url] = {"status": "done", "info": info, "county": county, "target": str(target.relative_to(ARCHIVE_ROOT))}
                return True

            target.write_bytes(content)
            log.info(f"  [✓] {target.relative_to(ARCHIVE_ROOT)} ({len(content)} bytes)")
            state[paper_url] = {
                "status": "done",
                "info": info,
                "county": county,
                "target": str(target.relative_to(ARCHIVE_ROOT)),
                "size": len(content),
            }
            return True

    except Exception as e:
        log.warning(f"  [download-error] {paper_url}: {str(e)[:100]}")
        state[paper_url] = {"status": "download_error", "info": info, "county": county, "error": str(e)}
        return False


async def main_async(batch_size: int, dry_run: bool):
    log = setup_logging()
    log.info(f"=== ExamBank archive 開始 (batch={batch_size}, dry_run={dry_run}) ===")

    # 抓 sitemap
    paper_urls = fetch_sitemap(log)
    if not paper_urls:
        log.error("Sitemap 抓不到, exit")
        return

    # Load state
    state = load_state()
    pending = [u for u in paper_urls if state.get(u, {}).get("status") != "done"]
    log.info(f"  Total: {len(paper_urls)}, done: {len(paper_urls) - len(pending)}, pending: {len(pending)}")

    # 只跑 batch_size 個 (預設 100)
    todo = pending[:batch_size]
    log.info(f"  這次跑: {len(todo)} papers")

    if dry_run:
        log.info("  DRY RUN — no actual downloads")
        for url in todo[:10]:  # 只 sample 10
            await archive_paper(None, url, state, log, dry_run=True)
        return

    # Playwright + httpx
    from playwright.async_api import async_playwright

    success_count = 0
    fail_count = 0
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()

        for i, url in enumerate(todo, 1):
            log.info(f"  [{i}/{len(todo)}] {url.split('/')[-1][:60]}")
            ok = await archive_paper(page, url, state, log, dry_run=False)
            if ok:
                success_count += 1
            else:
                fail_count += 1

            # 每 10 個 save state 一次
            if i % 10 == 0:
                save_state(state)
                log.info(f"    [progress] success={success_count}, fail={fail_count}")

            # Politeness delay
            await asyncio.sleep(1.0)

        await browser.close()

    # Final save
    save_state(state)
    log.info(f"\n=== Done: success={success_count}, fail={fail_count} ===")
    log.info(f"  state file: {STATE_FILE}")


def main():
    parser = argparse.ArgumentParser(description="Archive ExamBank 全國考卷 → StudyArk 結構")
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH,
                        help=f"一次跑的 papers 數 (default {DEFAULT_BATCH})")
    parser.add_argument("--dry-run", action="store_true",
                        help="只 parse URL,不實際下載")
    parser.add_argument("--resume", action="store_true",
                        help="從 state file 繼續跑 (default 行為,留 flag 給 explicit)")
    args = parser.parse_args()

    asyncio.run(main_async(args.batch, args.dry_run))


if __name__ == "__main__":
    main()