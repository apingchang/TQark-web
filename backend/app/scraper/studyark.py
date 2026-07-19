"""
StudyArk 搜尋 + 下載

策略(2026-07-19 改版):
- 搜尋:用 httpx + StudyArk cookies(從 .env 設定的 path 讀)
- 下載:用 httpx,從 StudyArk 直接 stream 到 user(不存 server disk)
- Server 只存 metadata 到 DB(DownloadHistory table)
- User 瀏覽器收到 Content-Disposition: attachment 自動存到 Downloads

StudyArk endpoints:
- search:  https://www.studyark.org/e/extend/shijuan/search.php
- token:   https://www.studyark.org/e/DownSys/download/get_token.php
- file:    https://www.studyark.org/e/DownSys/download/file.php?token=...
"""

import json
import re
import urllib.parse
from dataclasses import dataclass

import httpx

from app.config import settings


SEARCH_URL = "https://www.studyark.org/e/extend/shijuan/search.php"
GET_TOKEN_URL = "https://www.studyark.org/e/DownSys/download/get_token.php"
DOWNLOAD_URL = "https://www.studyark.org/e/DownSys/download/file.php"

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


# ============================================================
# Cookie 管理
# ============================================================
def load_cookies() -> dict[str, str]:
    """
    從 settings.cookies_path 讀 StudyArk cookies。
    支援兩種格式:
    - list of dict: [{name, value, domain, ...}]
    - dict: {name: value}
    """
    cookies_path = settings.cookies_path
    if not cookies_path.exists():
        raise FileNotFoundError(
            f"Cookies file not found: {cookies_path}\n"
            f"請先用 Playwright login 流程產生 cookies"
        )
    with open(cookies_path) as f:
        data = json.load(f)

    if isinstance(data, list):
        return {c["name"]: c["value"] for c in data}
    return data


def make_cookie_string(cookies: dict[str, str]) -> str:
    """組 cookie header"""
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


# ============================================================
# 搜尋
# ============================================================
def _build_search_params(
    grade: str | None = None,
    subject: str | None = None,
    school_year: str | None = None,
    school_term: str | None = None,
    exam_type: str | None = None,
    version: str | None = None,
    daan: str | None = None,
    page: int = 1,
) -> dict:
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
    """搜尋考古題,回傳 StudyArk JSON response"""
    cookies = load_cookies()
    params = _build_search_params(
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


# ============================================================
# ExamItem metadata(來自 search response 或自己組)
# ============================================================
@dataclass
class ExamItem:
    classid: str
    fileid: str
    filetype: str = "paper"  # paper / daan (StudyArk API convention)
    title: str = ""
    school_name: str = ""
    grade: str = ""
    school_year: str = ""
    school_term: str = ""
    category: str = ""
    subject: str = ""
    exam_type: str = ""
    version: str = ""

    @classmethod
    def from_search_result(cls, item: dict, filetype: str = "paper") -> "ExamItem":
        """從 StudyArk search 結果的 dict 組 ExamItem"""
        return cls(
            classid=str(item.get("classid", "")),
            fileid=str(item.get("id", "")),
            filetype=filetype,
            title=item.get("title", ""),
            school_name=item.get("school_name", ""),
            grade=item.get("grade", ""),
            school_year=item.get("school_year", ""),
            school_term=item.get("school_term", "") or _infer_term(item.get("title", "")),
            category=item.get("category", ""),
            subject=item.get("subject", ""),
            exam_type=item.get("type", ""),
            version=item.get("version", ""),
        )


def _infer_term(title: str) -> str:
    """從 title 推學期(若 API 沒給)"""
    if "上學期" in title:
        return "上學期"
    if "下學期" in title:
        return "下學期"
    return ""


# ============================================================
# 下載
# ============================================================
async def get_download_token(classid: str, fileid: str, filetype: str = "paper") -> str:
    """
    拿 download token。
    filetype: paper(試卷) / daan(答案)
    回傳 token string。
    """
    cookies = load_cookies()
    data = urllib.parse.urlencode(
        {"fileid": fileid, "classid": classid, "filetype": filetype}
    ).encode()

    headers = {
        "User-Agent": USER_AGENT,
        "Cookie": make_cookie_string(cookies),
        "Referer": f"https://www.studyark.org/e/DownSys/download/?classid={classid}&id={fileid}",
        # StudyArk 後端需要 Content-Type 才認得 form data(httpx 預設不送)
        "Content-Type": "application/x-www-form-urlencoded",
    }

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(GET_TOKEN_URL, data=data, headers=headers)
        resp.raise_for_status()
        token_resp = resp.json()

    token = token_resp.get("token")
    if not token:
        raise ValueError(f"No token in response: {token_resp}")
    return token


async def download_pdf_stream(classid: str, fileid: str, filetype: str = "paper"):
    """
    下載 PDF,回傳 (bytes, content_type)。

    不存 server disk — 直接從 StudyArk 抓 bytes。
    若要 streaming 可以改成 StreamingResponse,目前先 bytes(考古題 PDF 通常 < 5MB)。
    """
    token = await get_download_token(classid, fileid, filetype)

    cookies = load_cookies()
    headers = {
        "User-Agent": USER_AGENT,
        "Cookie": make_cookie_string(cookies),
        "Referer": "https://www.studyark.org/e/DownSys/download/",
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(f"{DOWNLOAD_URL}?token={token}", headers=headers)
        resp.raise_for_status()
        return resp.content, "application/pdf"


# ============================================================
# 檔名產生(給 user 下載用)
# ============================================================
_INVALID_FN_CHARS = re.compile(r'[\\/:\*\?"<>\|\x00-\x1f]')


def sanitize_filename(name: str) -> str:
    """去掉檔名非法字元"""
    # 把 / 等換成全形,避免誤判
    name = name.replace("/", "／").replace("\\", "＼").replace(":", "：")
    name = name.replace("*", "＊").replace("?", "？").replace('"', "＂")
    name = name.replace("<", "＜").replace(">", "＞").replace("|", "｜")
    # 去掉控制字元
    name = _INVALID_FN_CHARS.sub("", name)
    return name.strip()


def build_download_filename(item: ExamItem) -> str:
    """
    給 user 下載用的檔名。
    範例:市立石門國中_八年級_113上學期_語文領域_國文_第一次段考_翰林_試卷.pdf
    """
    # 從 title 推更完整的學期標示(若 API 沒給 school_term)
    title = item.title or ""

    parts = []
    if item.school_name:
        parts.append(item.school_name)
    if item.grade:
        parts.append(item.grade)
    if item.school_year:
        term = item.school_term or _infer_term(title)
        parts.append(f"{item.school_year}{term}" if term else item.school_year)
    if item.category and item.category != item.subject:
        parts.append(item.category)
    if item.subject:
        parts.append(item.subject)
    if item.exam_type:
        parts.append(item.exam_type)
    if item.version:
        parts.append(item.version)
    parts.append("答案" if item.filetype == "daan" else "試卷")

    name = "_".join(parts) + ".pdf"
    return sanitize_filename(name)