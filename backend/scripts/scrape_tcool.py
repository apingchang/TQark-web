"""
tcool.cc 學校考古題目錄聚合 (2026-07-26 新)

任務:
- 抓 https://www.tcool.cc/ 首頁
- 解析出「學校 + 縣市 + 學段 + 連結 + 連結類型」
- 寫到 backend/data/external_sources.json

用法:
  uv run python scripts/scrape_tcool.py

輸出: backend/data/external_sources.json (含 113 個學校連結)
"""
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urljoin

import requests
from bs4 import BeautifulSoup

# === Config ===
TCOOL_URL = "https://www.tcool.cc/"
OUTPUT_FILE = Path(__file__).parent.parent / "data" / "external_sources.json"

# 學段分類 (跟首頁 h3/h2 對應)
CATEGORY_PATTERNS = [
    ("junior", r"國中段考考古題"),
    ("senior", r"高中段考考古題"),
    ("elementary", r"國小段考考古題"),
]

# 縣市 alias (從學校名 parse 出縣市)
COUNTY_PATTERNS = [
    "台北市", "新北市", "桃園市", "台中市", "台南市", "高雄市",
    "基隆市", "新竹市", "嘉義市",
    "新竹縣", "苗栗縣", "彰化縣", "南投縣", "雲林縣", "嘉義縣",
    "屏東縣", "宜蘭縣", "花蓮縣", "台東縣", "澎湖縣", "金門縣", "連江縣",
]

# 學校的縣市 prefix regex
SCHOOL_COUNTY_RE = re.compile(r"^(台北市|新北市|桃園市|台中市|台南市|高雄市|基隆市|新竹市|嘉義市|新竹縣|苗栗縣|彰化縣|南投縣|雲林縣|嘉義縣|屏東縣|宜蘭縣|花蓮縣|台東縣|澎湖縣|金門縣|連江縣)")


def classify_link_type(url: str) -> str:
    """分類連結類型 (drive / school_web / nas / sharepoint / sites)"""
    host = urlparse(url).hostname or ""
    if "drive.google.com" in host or "docs.google.com" in host:
        return "drive"
    if "sharepoint.com" in host or "my.sharepoint" in host:
        return "sharepoint"
    if "sites.google.com" in host:
        return "sites"
    if ".edu.tw" in host:
        if "nas" in host or ":5001" in url:
            return "nas"
        return "school_web"
    return "other"


def parse_county(school_name: str) -> str | None:
    """從學校名 parse 縣市"""
    m = SCHOOL_COUNTY_RE.match(school_name)
    if m:
        return m.group(1)
    # fallback: alias
    alias = {
        "台北": "台北市", "北市": "台北市",
        "新北": "新北市",
        "桃園": "桃園市",
        "新竹": "新竹市",  # default 縣市
        "台中": "台中市", "中市": "台中市",
        "彰化": "彰化縣",
        "嘉義": "嘉義市",
        "台南": "台南市", "南市": "台南市",
        "高雄": "高雄市",
        "屏東": "屏東縣",
        "宜蘭": "宜蘭縣",
        "花蓮": "花蓮縣",
        "台東": "台東縣",
        "基隆": "基隆市",
        "南投": "南投縣",
        # 高級中學單科特別處理
        "建中": "台北市",         # 建國中學
        "北一女": "台北市",
        "雄中": "高雄市",         # 高雄中學
        "金門": "金門縣",
        "興大附": "台中市",       # 國立中興大學附屬高級中學
        "蘭陽女": "宜蘭縣",
        "竹女": "新竹市",
        "竹男": "新竹市",         # 新竹高中
        "武陵": "桃園市",
        "彰中": "彰化縣",
        "附中": None,             # 需進一歩規則
    }
    for short, full in alias.items():
        if full is None:
            continue
        if school_name.startswith(short):
            return full
    return None


def scrape_tcool() -> dict:
    """抓 tcool.cc 並解析出結構化資料"""
    print(f"[scrape_tcool] Fetching {TCOOL_URL} ...")
    resp = requests.get(TCOOL_URL, timeout=30, headers={
        "User-Agent": "TQark-web-aggregator/1.0 (+https://github.com/apingchang)"
    })
    resp.raise_for_status()
    html = resp.text

    soup = BeautifulSoup(html, "html.parser")

    # 結果
    sources = {
        "last_scraped": datetime.now(timezone.utc).isoformat(),
        "source_url": TCOOL_URL,
        "schools": [],
    }

    # 網頁用 <details><summary> + <ul>結構: summary 為分類名稱, sibling ul 為學校列表
    # 找所有 <summary> + sibling <ul>
    for summary in soup.find_all("summary"):
        text = summary.get_text(strip=True)
        current_category = None
        for cat_id, pattern in CATEGORY_PATTERNS:
            if re.search(pattern, text):
                current_category = cat_id
                break
        if not current_category:
            continue

        # 找 sibling ul (在 parent <details> 內)
        details = summary.parent
        ul = details.find("ul")
        if not ul:
            continue

        for li in ul.find_all("li", recursive=False):
            a = li.find("a")
            if not a:
                continue
            school_name = a.get_text(strip=True)
            url = a.get("href", "")
            if not school_name or not url:
                continue

            # Skip 「會考歷屆」「學測歷屆」(這些已有 CAP/CEEC 整合)
            if "會考" in school_name and "歷屆" in school_name:
                continue
            if "學測" in school_name and "歷屆" in school_name:
                continue

            county = parse_county(school_name)
            link_type = classify_link_type(url)
            has_answer = "noanswer" not in (li.get("class") or [])

            sources["schools"].append({
                "name": school_name,
                "county": county,
                "category": current_category,
                "url": url,
                "link_type": link_type,
                "has_answer": has_answer,
            })

    # Summary
    print(f"\n[scrape_tcool] Found {len(sources['schools'])} schools:")
    for cat_id, _ in CATEGORY_PATTERNS:
        count = sum(1 for s in sources["schools"] if s["category"] == cat_id)
        print(f"  {cat_id}: {count}")

    # by link type
    from collections import Counter
    type_counts = Counter(s["link_type"] for s in sources["schools"])
    print(f"\n[scrape_tcool] Link types:")
    for t, c in type_counts.most_common():
        print(f"  {t}: {c}")

    return sources


def main():
    try:
        data = scrape_tcool()
    except Exception as e:
        print(f"❌ Scrape failed: {e}", file=sys.stderr)
        sys.exit(1)

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[scrape_tcool] ✓ Wrote {len(data['schools'])} schools to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()