"""
考題收集 archive path helpers (2026-07-20 新增, 2026-07-21 加 county)。

Folder 結構:
/mnt/my_book/考題收集/
├── <county>/                         # 縣市 (e.g. 彰化縣, 桃園市)
│   └── 國小/                         # 一年級 ~ 六年級
│       └── {年級}/{科目}/{paper|daan}/<county>_{檔名}.pdf
│   └── 國中/
│   └── 高中/
├── 其他X/                            # county unknown / OCR 失敗 (其他X_)
"""
import os
from pathlib import Path

# 預設 archive 根目錄(可透過 env var 覆寫)
DEFAULT_ARCHIVE_DIR = "/mnt/my_book/考題收集"

# County unknown 的特殊 prefix (3 字寬)
UNKNOWN_COUNTY = "其他X"

# 學段對照(grade value → 學段)
SEGMENT_MAP = {
    "一年級": "國小", "二年級": "國小", "三年級": "國小",
    "四年級": "國小", "五年級": "國小", "六年級": "國小",
    "七年級": "國中", "八年級": "國中", "九年級": "國中",
    "十年級": "高中", "十一年級": "高中", "十二年級": "高中",
}

# 各學段的年級清單(用於建資料夾 + 驗證)
SEGMENT_GRADES = {
    "國小": ["一年級", "二年級", "三年級", "四年級", "五年級", "六年級"],
    "國中": ["七年級", "八年級", "九年級"],
    "高中": ["十年級", "十一年級", "十二年級"],
}


def get_archive_root() -> Path:
    """取得 archive 根目錄。可透過 TQARK_ARCHIVE_DIR env var 覆寫。"""
    return Path(os.environ.get("TQARK_ARCHIVE_DIR", DEFAULT_ARCHIVE_DIR))


def get_segment(grade: str) -> str | None:
    """從年級字串推算學段。"""
    return SEGMENT_MAP.get(grade)


def normalize_county(county: str | None) -> str:
    """正規化 county name, unknown → 其他X (3 字寬)"""
    if not county or county in ("未註明", "其他縣市", "其他X", "未知"):
        return UNKNOWN_COUNTY
    return county


def build_archive_path(
    grade: str,
    subject: str,
    filetype: str,  # "paper" or "daan"
    filename: str,
    county: str | None = None,
) -> Path | None:
    """
    組出 PDF 應該存放的完整路徑。

    Args:
        grade: StudyArk grade 值,例如「七年級」
        subject: StudyArk subject 值,例如「數學」或「國文」
        filetype: "paper" 或 "daan"
        filename: 已經組好的檔名(不含 .pdf,這裡會自己加)
        county: 縣市 (e.g. 彰化縣, 桃園市), None/未知 → 其他X

    Returns:
        完整 Path,如果學段推不出來 → None
    """
    segment = get_segment(grade)
    if not segment:
        return None
    if filetype not in ("paper", "daan"):
        return None
    if not subject:
        return None

    county_norm = normalize_county(county)
    safe_subject = _safe_dirname(subject)

    return (
        get_archive_root()
        / county_norm
        / segment
        / grade
        / safe_subject
        / filetype
        / f"{filename}.pdf"
    )


def build_archive_filename(
    county: str | None,
    year_term: str,
    exam_type: str,
    fileid: str,
    school_name: str,
    version: str,
) -> str:
    """
    組出 PDF 檔名 (不含 .pdf)
    
    Format: <county>_<year>_<exam>_<fileid>_<school>_<version>
    範例: 彰化縣_109_期末考_25441_縣立三潭國小_康軒
    
    Args:
        county: 縣市 (None/未知 → 其他X)
        year_term: e.g. "109下學期"
        exam_type: e.g. "期末考"
        fileid: StudyArk fileid (str)
        school_name: 完整學校名 (e.g. 縣立三潭國小)
        version: 版本 (e.g. 康軒)
    """
    county_norm = normalize_county(county)
    # 把有問題字元的部分 safe 化
    safe_school = school_name.replace("/", "／").replace(":", "：")
    safe_version = version.replace("/", "／").replace(":", "：") or "未註明"
    return f"{county_norm}_{year_term}_{exam_type}_{fileid}_{safe_school}_{safe_version}"


def _safe_dirname(name: str) -> str:
    """去掉資料夾名稱的非法字元,避免檔案系統錯誤。"""
    if not name:
        return "_未分類"
    # 替換常見非法字元
    name = name.replace("/", "／").replace("\\", "＼")
    name = name.replace(":", "：").replace("*", "＊")
    name = name.replace("?", "？").replace('"', "＂")
    name = name.replace("<", "＜").replace(">", "＞").replace("|", "｜")
    # 控制字元
    import re
    name = re.sub(r'[\x00-\x1f]', '', name)
    return name.strip() or "_未分類"


def ensure_archive_dirs(grade: str, subject: str, filetype: str, county: str | None = None) -> Path | None:
    """
    確保 archive 路徑上的所有資料夾都存在。

    Returns:
        archive 根目錄(成功) 或 None(失敗)
    """
    segment = get_segment(grade)
    if not segment or filetype not in ("paper", "daan") or not subject:
        return None

    county_norm = normalize_county(county)
    safe_subject = _safe_dirname(subject)
    target_dir = (
        get_archive_root()
        / county_norm
        / segment
        / grade
        / safe_subject
        / filetype
    )
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        return target_dir
    except OSError:
        return None


def find_pdf_in_archive(
    grade: str,
    subject: str,
    filetype: str,
    fileid: str,
    county: str | None = None,
) -> Path | None:
    """
    在 archive 裡找給定 fileid 的 PDF,回傳完整 Path 或 None。

    策略:
    - 已知 grade + subject + county → 直接看對應路徑 (快)
    - 不知道 county → 掃各 county folder 找
    - 不知道 subject → scan 整個學段 + 年級下所有科目資料夾找 {fileid}.pdf
    """
    if filetype not in ("paper", "daan"):
        return None

    segment = get_segment(grade)
    if not segment:
        return None

    archive_root = get_archive_root()
    safe_subject = _safe_dirname(subject) if subject else None

    # 1. 知道 county → 先看 county folder
    if county:
        county_norm = normalize_county(county)
        if safe_subject:
            candidate = (
                archive_root / county_norm / segment / grade / safe_subject
                / filetype / f"{fileid}.pdf"
            )
            if candidate.exists():
                return candidate
        # 不知道 subject → scan county folder 內找
        county_dir = archive_root / county_norm / segment / grade
        if county_dir.exists():
            for subject_dir in county_dir.iterdir():
                if not subject_dir.is_dir():
                    continue
                candidate = subject_dir / filetype / f"{fileid}.pdf"
                if candidate.exists():
                    return candidate

    # 2. 不知道 county → 掃所有 county folders
    if safe_subject:
        # 先在 所有 counties 找 (subject 已知 → county folder 內特定 path)
        for county_dir in archive_root.iterdir():
            if not county_dir.is_dir():
                continue
            if county_dir.name in ("state", "logs", "_trash"):
                continue
            candidate = (
                county_dir / segment / grade / safe_subject
                / filetype / f"{fileid}.pdf"
            )
            if candidate.exists():
                return candidate

    # 3. Slow path: 掃全部 county → segment → grade → subject
    for county_dir in archive_root.iterdir():
        if not county_dir.is_dir():
            continue
        if county_dir.name in ("state", "logs", "_trash"):
            continue
        grade_dir = county_dir / segment / grade
        if not grade_dir.exists():
            continue
        for subject_dir in grade_dir.iterdir():
            if not subject_dir.is_dir():
                continue
            candidate = subject_dir / filetype / f"{fileid}.pdf"
            if candidate.exists():
                return candidate

    return None


def get_state_path(name: str) -> Path:
    """取得 state/ 下某個狀態檔路徑。"""
    return get_archive_root() / "state" / name


def get_log_path(name: str) -> Path:
    """取得 logs/ 下某個 log 檔路徑。"""
    return get_archive_root() / "logs" / name