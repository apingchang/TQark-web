"""
考題收集 archive path helpers (2026-07-20 新增)。

Folder 結構:
/mnt/my_book/考題收集/
├── 國小/                         # 一年級 ~ 六年級
│   └── {年級}/{科目}/{paper|daan}/{檔名}.pdf
├── 國中/                         # 七年級 ~ 九年級
└── 高中/                         # 十年級 ~ 十二年級
"""
import os
from pathlib import Path

# 預設 archive 根目錄(可透過 env var 覆寫)
DEFAULT_ARCHIVE_DIR = "/mnt/my_book/考題收集"

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


def build_archive_path(
    grade: str,
    subject: str,
    filetype: str,  # "paper" or "daan"
    filename: str,
) -> Path | None:
    """
    組出 PDF 應該存放的完整路徑。

    Args:
        grade: StudyArk grade 值,例如「七年級」
        subject: StudyArk subject 值,例如「數學」或「國文」
        filetype: "paper" 或 "daan"
        filename: 已經組好的檔名(不含 .pdf,這裡會自己加)

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

    # 過濾 subject 不合法字元(避免資料夾有奇怪符號)
    safe_subject = _safe_dirname(subject)

    return (
        get_archive_root()
        / segment
        / grade
        / safe_subject
        / filetype
        / f"{filename}.pdf"
    )


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


def ensure_archive_dirs(grade: str, subject: str, filetype: str) -> Path | None:
    """
    確保 archive 路徑上的所有資料夾都存在。

    Returns:
        archive 根目錄(成功) 或 None(失敗)
    """
    segment = get_segment(grade)
    if not segment or filetype not in ("paper", "daan") or not subject:
        return None

    safe_subject = _safe_dirname(subject)
    target_dir = (
        get_archive_root()
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
) -> Path | None:
    """
    在 archive 裡找給定 fileid 的 PDF,回傳完整 Path 或 None。

    策略:
    - 已知 grade + subject → 直接看對應路徑 (快)
    - 不知道 subject → scan 整個學段 + 年級下所有科目資料夾找 {fileid}.pdf
      (用於 TQark-web 收到 search results 時,subject 可能是空的或與 archive 不一致)
    """
    if filetype not in ("paper", "daan"):
        return None

    segment = get_segment(grade)
    if not segment:
        return None

    archive_root = get_archive_root()

    if subject:
        # Fast path:直接到對應資料夾找
        safe_subject = _safe_dirname(subject)
        candidate = (
            archive_root
            / segment / grade / safe_subject / filetype / f"{fileid}.pdf"
        )
        if candidate.exists():
            return candidate

    # Slow path:scan 整個學段 + 年級找
    grade_dir = archive_root / segment / grade
    if not grade_dir.exists():
        return None
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
