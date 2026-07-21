"""
Rebuild school_stats.json from existing PDFs (用 PDF title 解析 county + school_name)

1. 掃描所有 PDF
2. 用 pdftotext 抓 title
3. 從 title parse county + school_name
4. 累積寫入 school_stats.json

不同於 build_school_stats.py 的 filename parse, 這個版本從 PDF 內容 parse (更準確)。
"""
import sys
import json
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.scraper.pdf_title import extract_school_from_pdf
from app.scraper.school_stats import (
    load_stats, save_stats, ARCHIVE_DIR
)


def main():
    pdf_files = list(ARCHIVE_DIR.rglob("*.pdf"))
    print(f"📂 Found {len(pdf_files)} PDFs")
    
    stats = load_stats()
    stats["schools"] = {}
    stats["counties"] = defaultdict(int)
    stats["by_county_school"] = defaultdict(lambda: defaultdict(int))
    stats["pdf_titles"] = []  # 新增:title 紀錄
    
    seen_fileids = set()
    ok = 0
    fail = 0
    
    for pdf_path in pdf_files:
        parts = pdf_path.parts
        if "paper" in parts:
            filetype = "paper"
        elif "daan" in parts:
            filetype = "daan"
        else:
            continue
        
        # 從 filename parse fileid
        filename = pdf_path.stem
        # Format: <year>_<exam_type>_<fileid>_<school>_<version>
        try:
            fparts = filename.split("_")
            fileid = fparts[2]
        except (IndexError, ValueError):
            continue
        
        # 從 path 拆 grade + subject
        rel = pdf_path.relative_to(ARCHIVE_DIR)
        # 國小/1年級/國語/paper/<file>.pdf
        grade = parts[len(ARCHIVE_DIR.parts)]
        subject_idx = parts.index(filetype) - 1
        subject = parts[subject_idx]
        
        # 從 PDF 抓 title
        info = extract_school_from_pdf(pdf_path)
        county = info["county"]
        school_name = info["school_name"]
        title = info["title"]
        
        stats["pdf_titles"].append({
            "fileid": fileid,
            "filetype": filetype,
            "title": title,
            "county": county,
            "school_name": school_name,
        })
        
        # School entry
        if school_name not in stats["schools"]:
            stats["schools"][school_name] = {
                "county": county,
                "school_short": info["school_short"],
                "grade": grade,
                "subject": subject,
                "fileids": [],
                "total_files": 0,
            }
        
        entry_key = f"{fileid}_{filetype}"
        if entry_key not in stats["schools"][school_name]["fileids"]:
            stats["schools"][school_name]["fileids"].append(entry_key)
            stats["schools"][school_name]["total_files"] += 1
        
        # County 統計
        stats["counties"][county] += 1
        
        # by_county_school
        if county not in stats["by_county_school"]:
            stats["by_county_school"][county] = {}
        if school_name not in stats["by_county_school"][county]:
            stats["by_county_school"][county][school_name] = 0
        stats["by_county_school"][county][school_name] += 1
        
        if county != "未註明":
            ok += 1
        else:
            fail += 1
    
    # 轉 default dict 為 dict
    stats["counties"] = dict(stats["counties"])
    stats["by_county_school"] = {k: dict(v) for k, v in stats["by_county_school"].items()}
    
    save_stats(stats)
    
    print(f"\n✅ Done. Success rate: {ok}/{ok+fail} ({100*ok/(ok+fail):.0f}%)")
    print(f"\nTop 15 schools:")
    sorted_schools = sorted(stats["schools"].items(), key=lambda x: -x[1]["total_files"])
    for name, info in sorted_schools[:15]:
        print(f"  {info['county']:6} | {info['total_files']:3} | {name}")


if __name__ == "__main__":
    main()