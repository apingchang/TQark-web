"""
StudyArk 搜尋 + 下載

把現有的 studyark_downloader.py 邏輯包成 async function,
讓 FastAPI 可以呼叫。

策略:
- 搜尋:用 httpx + StudyArk cookies(從 .env 設定的 path 讀)
- 下載:Playwright headless(因為 StudyArk 有 Cloudflare / JS 檢查)
- PDF 暫存:到 credentials/pdfs/<exam_id>.pdf

每個 function 都吃 cookies,確保測試期可換。
"""

import asyncio
import hashlib
import json
import urllib.parse
from pathlib import Path

import httpx

from app.config import settings


# StudyArk endpoints(從現有 scraper 抄過來)
SEARCH_URL = "https://www.studyark.org/e/extend/shijuan/search.php"
GET_TOKEN_URL = "https://www.studyark.org/e/DownSys/download/get_token.php"
DOWNLOAD_URL = "https://www.studyark.org/e/DownSys/download/file.php"

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def load_cookies() -> dict[str, str]:
    """
    從 settings.cookies_path 讀 StudyArk cookies。
    格式是 JSON dict(從之前 extract_cookies.py 產的)。
    """
    cookies_path = Path(settings.cookies_path)
    if not cookies_path.exists():
        raise FileNotFoundError(
            f"Cookies file not found: {cookies_path}\n"
            f"請先用 Playwright login 流程產生 cookies"
        )
    with open(cookies_path) as f:
        cookies_list = json.load(f)

    # cookies 可能是 list of dict 或 dict — 兩種都支援
    if isinstance(cookies_list, list):
        return {c["name"]: c["value"] for c in cookies_list}
    return cookies_list


def make_cookie_string(cookies: dict[str, str]) -> str:
    """組 cookie header"""
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def _search_params(
    grade: str | None = None,
    subject: str | None = None,
    school_year: str | None = None,
    school_term: str | None = None,
    exam_type: str | None = None,
    version: str | None = None,
    daan: str | None = None,
    page: int = 1,
) -> dict:
    """組 search params"""
    params = {}
    for k, v in [
        ("grade", grade),
        ("subject", subject),
        ("school_year", school_year),
        ("school_term", school_term),
        ("type", exam_type),
        ("version", version),
        ("daan", daan),
    ]:
        if v:
            params[k] = v
    params["page"] = page
    return params


async def search_papers(
    grade: str | None = None,
    subject: str | None = None,
    school_year: str | None = None,
    school_term: str | None = None,
    exam_type: str | None = None,
    version: str | None = None,
    daan: str | None = None,
    page: int = 1,
) -> dict:
    """
    搜尋考古題,回傳 StudyArk JSON response。

    params 對應 StudyArk 欄位:
    - grade: 七年級 / 八年級 / 九年級
    - subject: 國文 / 英語 / 數學 / 自然 / 社會
    - school_year: 113 / 112 / 111 / 110 ...
    - school_term: 上學期 / 下學期
    - exam_type: 第一次段考 / 第二次段考 / 第三次段考 / 期末考
    - version: 翰林 / 康軒 / 南一
    - daan: yes / no (有沒有附答案)
    """
    cookies = load_cookies()
    params = _search_params(
        grade=grade,
        subject=subject,
        school_year=school_year,
        school_term=school_term,
        exam_type=exam_type,
        version=version,
        daan=daan,
        page=page,
    )

    url = f"{SEARCH_URL}?{urllib.parse.urlencode(params)}"
    headers = {
        "User-Agent": USER_AGENT,
        "Cookie": make_cookie_string(cookies),
        "Referer": "https://www.studyark.org/",
    }

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()


async def get_download_token(classid: str, fileid: str, filetype: str = "paper") -> dict:
    """
    拿 download token(用來實際下載檔案)。
    filetype: paper(試卷) / answer(答案)
    """
    cookies = load_cookies()
    data = urllib.parse.urlencode(
        {"fileid": fileid, "classid": classid, "filetype": filetype}
    ).encode()

    headers = {
        "User-Agent": USER_AGENT,
        "Cookie": make_cookie_string(cookies),
        "Referer": f"https://www.studyark.org/e/DownSys/download/?classid={classid}&id={fileid}",
        # 明确指定 Content-Type,StudyArk 后端需要才会认 form data
        "Content-Type": "application/x-www-form-urlencoded",
    }

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(GET_TOKEN_URL, data=data, headers=headers)
        resp.raise_for_status()
        return resp.json()


async def download_pdf(classid: str, fileid: str, filetype: str = "paper") -> bytes:
    """下載 PDF binary"""
    token_resp = await get_download_token(classid, fileid, filetype)
    token = token_resp.get("token")
    if not token:
        raise ValueError(f"No token in response: {token_resp}")

    cookies = load_cookies()
    headers = {
        "User-Agent": USER_AGENT,
        "Cookie": make_cookie_string(cookies),
        "Referer": "https://www.studyark.org/e/DownSys/download/",
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(f"{DOWNLOAD_URL}?token={token}", headers=headers)
        resp.raise_for_status()
        return resp.content


def cache_key(classid: str, fileid: str, filetype: str = "paper") -> str:
    """產生 cache file name(用 SHA 避免特殊字元)"""
    h = hashlib.sha256(f"{classid}:{fileid}:{filetype}".encode()).hexdigest()[:16]
    return f"{filetype}_{h}.pdf"


async def download_to_cache(classid: str, fileid: str, filetype: str = "paper") -> Path:
    """
    下載 PDF 到 credentials/pdfs/<cache_key>,
    回傳 Path(已存在的檔案)。
    """
    cache_dir = Path(settings.pdf_cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    fname = cache_key(classid, fileid, filetype)
    fpath = cache_dir / fname

    if fpath.exists():
        return fpath

    pdf_bytes = await download_pdf(classid, fileid, filetype)
    fpath.write_bytes(pdf_bytes)
    return fpath