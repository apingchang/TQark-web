"""
CEEC (大學入學考試中心) scraper。

來源: https://www.ceec.edu.tw
- 學科能力測驗 (學測) 一般試題: ?xsmsid=0J052424829869345634
- 分科測驗 一般試題: ?xsmsid=0J052427633128416650
- 學測 特殊試題: ?xsmsid=0J052392083839398563
- 分科測驗 特殊試題: ?xsmsid=0J052424613319003165
- 高中英語聽力測驗 下載專區: ?xsmsid=0J052605494129871538

特色:
- 公開、免登入、無限流
- 直接 PDF 下載 URL (不用 Google Drive)
- 範圍: 學測 83-115 年 + 分科測驗 + 英聽

【2026-07-22 新增】
"""
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, quote

import requests
from bs4 import BeautifulSoup

# 把 backend 加進 path,讓 import app.* work
BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

BASE_URL = "https://www.ceec.edu.tw"

# 各類考試的 xsmsid
EXAM_CATEGORIES = {
    "學測_一般": "0J052424829869345634",
    "學測_特殊": "0J052392083839398563",
    "學測_特殊答題卷": "0M111357021798465239",
    "分科_一般": "0J052427633128416650",
    "分科_特殊": "0J052424613319003165",
    "分科_特殊答題卷": "0M111360260774151742",
    "英聽_下載專區": "0J052605494129871538",
}

# 學測科目 (一般試題)
GSAT_SUBJECTS = {
    "國綜": "國文",
    "國寫": "寫作測驗",
    "英文": "英語",
    "數A": "數學A",
    "數B": "數學B",
    "社會": "社會",
    "自然": "自然",
}


@dataclass
class CeecFile:
    """單一 PDF 檔案"""
    exam_type: str             # "學測" / "分科" / "英聽"
    category: str              # "一般" / "特殊" / "特殊答題卷" / "下載專區"
    year: int                  # 民國年
    subject: str               # 國文/英語/數學/社會/自然/寫作測驗/...
    file_type: str             # "試題" / "答題卷" / "答案" / "評分原則"
    url: str                   # PDF URL (CEEC 自家, 不需 redirect)
    title: str = ""            # 顯示名稱
    page: int = 0              # 在哪一頁抓到

    @property
    def display_name(self) -> str:
        return f"{self.year}_{self.exam_type}_{self.subject}_{self.file_type}"


def _fetch_utf8(session: requests.Session, url: str) -> str:
    """Fetch 並強制 UTF-8 解碼"""
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    return resp.text


def _list_xmfile_pages(xsmsid: str, max_pages: int = 30) -> list[str]:
    """
    列出指定 xsmsid 頁面的所有 PDF URL (含分頁)。
    CEEC 用 `?page=N` 做分頁。
    """
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0"

    all_pdf_urls = []
    for page in range(1, max_pages + 1):
        url = f"{BASE_URL}/xmfile?xsmsid={xsmsid}&page={page}"
        try:
            text = _fetch_utf8(session, url)
        except Exception as e:
            print(f"  ⚠️  page {page} failed: {e}")
            break

        # 找 PDF 連結
        soup = BeautifulSoup(text, "html.parser")
        page_pdfs = []
        for link in soup.find_all("a", href=True):
            href = link["href"]
            if href.endswith(".pdf") or ".pdf?" in href or ".pdf" in href.lower():
                # 處理相對 URL
                if href.startswith("/"):
                    href = BASE_URL + href
                elif not href.startswith("http"):
                    href = urljoin(url, href)
                page_pdfs.append(href)

        if not page_pdfs:
            break
        all_pdf_urls.extend(page_pdfs)
        print(f"  Page {page}: {len(page_pdfs)} PDFs")

        # 如果這個 page 沒有下一頁 link,停止
        if f"page={page + 1}" not in text and f"page={page + 1}" not in text.replace("&amp;", "&"):
            break

    return all_pdf_urls


def parse_gsat_filename(filename: str) -> dict:
    """
    Parse 學測/分科 PDF 檔名, 抽出 year, subject, file_type。
    範例:
      "01-115學測國綜試題卷.pdf" -> year=115, subject="國綜", file_type="試題卷"
      "01-115學測國綜答題卷.pdf" -> year=115, subject="國綜", file_type="答題卷"
      "115學測國文考科(國綜)非選擇題參考答案與評分原則.pdf" -> year=115, subject="國綜", file_type="評分原則"
    """
    # 從 URL 拿 decoded filename
    decoded = filename.replace(".pdf", "").replace("%20", " ")
    # 也試 unquote
    from urllib.parse import unquote
    decoded = unquote(filename).replace(".pdf", "")

    info = {
        "year": 0,
        "subject": "",
        "file_type": "",
    }

    # 找年份 (3 位民國年)
    m = re.search(r"(\d{3})學測", decoded) or re.search(r"(\d{3})分科", decoded)
    if m:
        info["year"] = int(m.group(1))

    # 找 file_type
    if "試題" in decoded or "試卷" in decoded:
        info["file_type"] = "試題"
    elif "答題卷" in decoded:
        info["file_type"] = "答題卷"
    elif "評分原則" in decoded:
        info["file_type"] = "評分原則"
    elif "答案" in decoded:
        info["file_type"] = "答案"

    # 找 subject (根據關鍵字)
    subject_patterns = [
        ("國綜", "國綜"),
        ("國寫", "國寫"),
        ("國語", "國綜"),  # 國語文 = 國綜
        ("國文", "國綜"),
        ("英文", "英文"),
        ("英語", "英文"),
        ("數A", "數A"),
        ("數B", "數B"),
        ("數學A", "數A"),
        ("數學B", "數B"),
        ("社會", "社會"),
        ("自然", "自然"),
        ("寫作", "國寫"),
    ]
    for kw, subj in subject_patterns:
        if kw in decoded:
            info["subject"] = subj
            break

    return info


def list_gsat_files(exam_type: str = "學測", category: str = "一般", max_pages: int = 30) -> list[CeecFile]:
    """
    列出學測/分科測驗的一般試題 PDF。
    """
    if category == "一般":
        key = f"{exam_type}_一般"
    elif category == "特殊":
        key = f"{exam_type}_特殊"
    elif category == "特殊答題卷":
        key = f"{exam_type}_特殊答題卷"
    else:
        raise ValueError(f"Unknown category: {category}")

    xsmsid = EXAM_CATEGORIES[key]
    print(f"Fetching {key} (xsmsid={xsmsid})...")

    pdf_urls = _list_xmfile_pages(xsmsid, max_pages=max_pages)

    files = []
    for i, url in enumerate(pdf_urls):
        # 拿檔名
        from urllib.parse import unquote
        decoded = unquote(url.split("/")[-1].split("?")[0])

        # 過濾不相關的 PDF
        if "高中英語聽力測驗常見問題" in decoded:
            continue

        info = parse_gsat_filename(decoded)

        files.append(CeecFile(
            exam_type=exam_type,
            category=category,
            year=info["year"],
            subject=info["subject"],
            file_type=info["file_type"],
            url=url,
            title=decoded,
            page=0,  # 都從同一個 page 來源,簡化
        ))

    return files


def list_all_ceec_files() -> list[CeecFile]:
    """
    列出所有 CEEC PDF。
    """
    all_files = []

    for exam_type in ["學測", "分科"]:
        for category in ["一般", "特殊", "特殊答題卷"]:
            try:
                files = list_gsat_files(exam_type, category, max_pages=25)
                print(f"  {exam_type} {category}: {len(files)} PDFs")
                all_files.extend(files)
            except Exception as e:
                print(f"  ⚠️  {exam_type} {category} 失敗: {e}")

    return all_files


def download_ceec_pdf(url: str, dest: Path) -> Path:
    """直接下載 CEEC PDF (不用 Google Drive 跳轉)"""
    import requests
    dest.parent.mkdir(parents=True, exist_ok=True)

    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    }

    with requests.get(url, headers=headers, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
    return dest


if __name__ == "__main__":
    # Test: list 學測一般
    print("=== 學測 一般試題 ===")
    files = list_gsat_files("學測", "一般", max_pages=20)
    print(f"Total: {len(files)}")
    for f in files[:5]:
        print(f"  {f.year} | {f.subject} | {f.file_type} | {f.title}")
