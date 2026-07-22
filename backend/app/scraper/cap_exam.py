"""
CAP Exam (Comprehensive Assessment Program for Junior High School Students)
歷屆國中教育會考試題 scraper。

來源: https://cap.rcpet.edu.tw/examination.html
        (RCPET 國立臺灣師範大學心理與教育測驗研究發展中心)

特色:
- 公開、不需登入、沒有 rate limit
- 所有 PDF 都在 Google Drive
- 範圍: 102-115 年 + 國中基測

【2026-07-22 新增】
"""
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# 把 backend 加進 path,讓 import app.* work
BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

BASE_URL = "https://cap.rcpet.edu.tw"
EXAMINATION_URL = f"{BASE_URL}/examination.html"

# 科目對照
SUBJECT_ALIASES = {
    "國文": "國文",
    "國語": "國文",
    "英語": "英語",
    "英文": "英語",
    "數學": "數學",
    "社會": "社會",
    "自然": "自然",
    "寫作": "寫作測驗",
    "寫作測驗": "寫作測驗",
    "參考答案": "參考答案",
    "試題說明": "試題說明",
}


@dataclass
class CapExamFile:
    """單一 PDF 檔案"""
    year: int                  # 民國年, 例如 115
    exam_type: str             # "會考" 或 "基測"
    subject: str               # 國文/英語/數學/社會/自然/寫作測驗/參考答案/試題說明
    file_id: str               # Google Drive file_id
    title: str = ""            # 顯示名稱, 例如 "01-115學測國綜試題"
    url: str = ""              # Google Drive view URL
    pdf_url: str = ""          # 直接 PDF URL

    @property
    def download_url(self) -> str:
        """Google Drive 直接下載 URL"""
        return f"https://drive.google.com/uc?export=download&id={self.file_id}"


def _fetch_utf8(session: requests.Session, url: str) -> str:
    """
    Fetch URL 並保證解碼為 UTF-8 字串。

    重要: RCPET 網站 HTTP header 沒有指定 charset,
    requests 預設會用 ISO-8859-1 來解碼,導致中文變亂碼 (mojibake)。
    所以要顯式告訴 requests 用 UTF-8。
    """
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    resp.encoding = "utf-8"  # 強制 UTF-8,即使 header 說別的
    return resp.text


def list_year_exam_files(year: int, session: Optional[requests.Session] = None) -> list[CapExamFile]:
    """
    抓某年的會考試題頁面,parse 出所有 PDF。

    頁面結構:
      https://cap.rcpet.edu.tw/exam/{year}/{year}exam.html
      <a href="https://drive.google.com/file/d/{FILE_ID}/view?usp=drive_link">{title}</a>
    """
    if session is None:
        session = requests.Session()
        session.headers["User-Agent"] = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"

    # 102 年是「試辦」, URL 略不同
    if year == 102:
        url = f"{BASE_URL}/exam/102/102exam.html"
    else:
        url = f"{BASE_URL}/exam/{year}/{year}exam.html"

    text = _fetch_utf8(session, url)
    soup = BeautifulSoup(text, "html.parser")
    files = []

    for link in soup.find_all("a", href=True):
        href = link["href"]
        if "drive.google.com/file/d/" not in href:
            continue
        m = re.search(r"/file/d/([a-zA-Z0-9_-]+)", href)
        if not m:
            continue
        file_id = m.group(1)
        title = link.get_text(strip=True) or ""

        # 判斷 subject
        subject = ""
        for kw, subj in SUBJECT_ALIASES.items():
            if kw in title:
                subject = subj
                break
        if not subject:
            subject = "其他"

        files.append(CapExamFile(
            year=year,
            exam_type="會考",
            subject=subject,
            file_id=file_id,
            title=title,
            url=href,
        ))

    return files


def list_bctest_files(session: Optional[requests.Session] = None) -> list[CapExamFile]:
    """國中基測 (BCTEST) 試題,單頁列表"""
    if session is None:
        session = requests.Session()
        session.headers["User-Agent"] = "Mozilla/5.0"

    url = f"{BASE_URL}/BCTESTexam.html"
    text = _fetch_utf8(session, url)
    soup = BeautifulSoup(text, "html.parser")

    files = []
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if "drive.google.com/file/d/" not in href:
            continue
        m = re.search(r"/file/d/([a-zA-Z0-9_-]+)", href)
        if not m:
            continue
        file_id = m.group(1)
        title = link.get_text(strip=True) or ""
        subject = ""
        for kw, subj in SUBJECT_ALIASES.items():
            if kw in title:
                subject = subj
                break
        if not subject:
            subject = "其他"

        files.append(CapExamFile(
            year=0,
            exam_type="基測",
            subject=subject,
            file_id=file_id,
            title=title,
            url=href,
        ))
    return files


def list_all_cap_files(session: Optional[requests.Session] = None) -> list[CapExamFile]:
    """
    列出所有 CAP 會考 + 基測 PDF。
    範圍: 102-115 年會考 + 基測 (BCTEST)。
    """
    if session is None:
        session = requests.Session()
        session.headers["User-Agent"] = "Mozilla/5.0"

    all_files = []
    for year in range(102, 116):  # 102 to 115 inclusive
        try:
            files = list_year_exam_files(year, session)
            print(f"  {year} 年: {len(files)} PDFs")
            all_files.extend(files)
        except Exception as e:
            print(f"  ⚠️  {year} 年失敗: {e}")

    try:
        bctest = list_bctest_files(session)
        print(f"  基測 (BCTEST): {len(bctest)} PDFs")
        all_files.extend(bctest)
    except Exception as e:
        print(f"  ⚠️  基測失敗: {e}")

    return all_files


def download_gdrive(file_id: str, dest: Path, quiet: bool = False) -> Path:
    """
    直接用 requests 下載 Google Drive 檔案 (不用 gdown 的 metadata API)。

    gdown 會先 call Google Drive API 拿 file metadata,如果短時間太多 request
    會被限流 ("Cannot retrieve the public link of the file")。
    直接用 requests + 標準 download URL 就避開這個問題。

    Args:
        file_id: Google Drive file ID
        dest: 目標路徑
        quiet: 是否安靜模式

    Returns:
        實際下載到的檔案路徑
    """
    import requests
    url = f"https://drive.google.com/uc?export=download&id={file_id}"
    dest.parent.mkdir(parents=True, exist_ok=True)

    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    }

    with requests.get(url, headers=headers, stream=True, timeout=60, allow_redirects=True) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
    return dest


if __name__ == "__main__":
    # CLI test
    print("Listing CAP exam files...")
    files = list_all_cap_files()
    print(f"\nTotal: {len(files)} PDFs")

    # Group by year
    by_year = {}
    for f in files:
        key = (f.year, f.exam_type)
        by_year.setdefault(key, []).append(f)
    for (year, exam), items in sorted(by_year.items()):
        print(f"  {exam} {year} 年: {len(items)} PDFs")
