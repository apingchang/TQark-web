"""
School + County 統計工具

從 StudyArk search response 的 school_name 解析縣市 + 學校,
寫進 school_stats.json (累積統計)。

用法:
    from app.scraper.school_stats import update_school_stats
    
    update_school_stats(
        fileid=42642,
        school_name="臺北市立內湖國民中學",
        grade="9年級",
        subject="理化",
        filetype="paper",
    )
"""
import json
import re
from datetime import datetime
from pathlib import Path

import zoneinfo

ARCHIVE_DIR = Path("/mnt/my_book/考題收集")
STATS_FILE = ARCHIVE_DIR / "state" / "school_stats.json"

TZ_TAIPEI = zoneinfo.ZoneInfo("Asia/Taipei")

# 台灣縣市 prefix (含 桃園縣 舊名 alias, 2014 前升格前為「桃園縣立」)
COUNTY_PATTERNS = [
    "臺北市", "台北市",  # 簡體 "台" 也支援 (常見於 PDF OCR)
    "北市",  # OCR 常抓 "北市南港區..." 形式
    "新北市",
    "桃園市", "桃園縣",  # 舊名 alias (2014 前升格)
    "臺中市", "台中市",
    "臺南市", "台南市",
    "高雄市",
    "基隆市",
    "新竹市", "新竹縣",
    "嘉義市", "嘉義縣",
    "苗栗縣", "彰化縣", "南投縣", "雲林縣",
    "屏東縣", "宜蘭縣", "花蓮縣", "臺東縣", "澎湖縣", "金門縣", "連江縣",
]

# County normalization (舊名/簡體 → 新名/正體)
COUNTY_ALIASES = {
    "桃園縣": "桃園市",  # 2014 升格
    "台北市": "臺北市",  # 簡體 → 正體
    "北市": "臺北市",  # OCR 拼接誤抓 ("北市南港區...")
    "台中市": "臺中市",
    "台南市": "臺南市",
}


def parse_county(school_name: str) -> str:
    """從學校名稱解析縣市"""
    if not school_name:
        return "其他縣市"
    for county in COUNTY_PATTERNS:
        if school_name.startswith(county):
            # Normalize 舊名
            return COUNTY_ALIASES.get(county, county)
    return "其他縣市"


def parse_school_short(school_name: str) -> str:
    """去掉縣市 prefix, 留下學校簡名 (例如 '臺北市立內湖國民中學' → '內湖國民中學')"""
    if not school_name:
        return "未註明"
    for county in COUNTY_PATTERNS:
        if school_name.startswith(county):
            return school_name[len(county):]
    return school_name


def load_stats() -> dict:
    if STATS_FILE.exists():
        return json.loads(STATS_FILE.read_text())
    return {
        "schools": {},      # {school_name: {county, fileids: [], count}}
        "counties": {},     # {county: count}
        "by_county_school": {},  # {county: {school_name: count}}
        "last_updated": None,
    }


def save_stats(stats: dict):
    STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
    stats["last_updated"] = datetime.now(TZ_TAIPEI).isoformat()
    STATS_FILE.write_text(json.dumps(stats, indent=2, ensure_ascii=False))


def update_school_stats(fileid: int | str, school_name: str, grade: str, subject: str, filetype: str):
    """累積一個 paper 的學校/縣市統計"""
    stats = load_stats()
    fileid_str = str(fileid)
    entry_key = f"{fileid_str}_{filetype}"

    # 先檢查是否已有這個 fileid (跨學校名 dedupe)
    existing_school_key = None
    for sk, info in stats["schools"].items():
        if entry_key in info.get("fileids", []):
            existing_school_key = sk
            break

    # 解析 county + short school name
    county = parse_county(school_name)
    school_short = parse_school_short(school_name)

    # 如果有完整 school_name (有 county), 用新的 key (去掉 "(from filename)" 標記)
    # 如果只有短名, 用既有 key (保留 retroactively 建的)
    if existing_school_key and "(from filename)" in existing_school_key:
        # 從 retroactively key 升級到完整 key
        new_school_key = school_name or existing_school_key
        if new_school_key != existing_school_key:
            # 把 fileid 從舊 key 搬到新 key
            old_info = stats["schools"].pop(existing_school_key)
            if new_school_key not in stats["schools"]:
                stats["schools"][new_school_key] = {
                    "county": county,
                    "school_short": school_short,
                    "grade": grade,
                    "fileids": [],
                    "total_files": 0,
                }
            stats["schools"][new_school_key]["fileids"] = old_info["fileids"]
            stats["schools"][new_school_key]["total_files"] = len(old_info["fileids"])
            stats["schools"][new_school_key]["grade"] = grade
            school_key = new_school_key
        else:
            # 同一個學校 (只是去掉 from filename 標記)
            old_info = stats["schools"].pop(existing_school_key)
            stats["schools"][new_school_key] = old_info
            stats["schools"][new_school_key]["county"] = county
            stats["schools"][new_school_key]["school_short"] = school_short
            school_key = new_school_key
    else:
        # 新學校 or 已存在的學校 (跨學校同名)
        school_key = school_name or "未註明"
        if school_key not in stats["schools"]:
            stats["schools"][school_key] = {
                "county": county,
                "school_short": school_short,
                "grade": grade,
                "fileids": [],
                "total_files": 0,
            }

    school_entry = stats["schools"][school_key]
    # 更新 county (可能有新資訊)
    if county != "未註明":
        school_entry["county"] = county
    school_entry["school_short"] = school_short

    # fileid 去重
    if entry_key not in school_entry["fileids"]:
        school_entry["fileids"].append(entry_key)
        school_entry["total_files"] = len(school_entry["fileids"])

    # 縣市統計 (數 paper + daan)
    final_county = school_entry["county"]
    stats["counties"][final_county] = stats["counties"].get(final_county, 0) + 1

    # county × school 統計
    if final_county not in stats["by_county_school"]:
        stats["by_county_school"][final_county] = {}
    if school_key not in stats["by_county_school"][final_county]:
        stats["by_county_school"][final_county][school_key] = 0
    stats["by_county_school"][final_county][school_key] += 1

    save_stats(stats)


def get_summary() -> dict:
    """取得摘要統計"""
    stats = load_stats()
    return {
        "total_schools": len(stats["schools"]),
        "total_files": sum(s["total_files"] for s in stats["schools"].values()),
        "counties": dict(sorted(stats["counties"].items(), key=lambda x: -x[1])),
    }


if __name__ == "__main__":
    # 測試
    summary = get_summary()
    print(f"Total schools: {summary['total_schools']}")
    print(f"Total files: {summary['total_files']}")
    print(f"By county:")
    for county, count in summary["counties"].items():
        print(f"  {county}: {count}")