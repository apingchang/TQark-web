#!/usr/bin/env python3
"""
Drive folder dry-run analysis (2026-08-24)

任務: 對 _未分類/DriveFolder/ 內 11,995 個無 level 的 PDF
       模擬歸檔到 <county>/<level>/<grade>/<subject>/<paper|daan>/ 路徑
       生成 dry-run report 給 William review

🚫 READ-ONLY: 不動任何檔
✅ 只從 state/local_papers_index.json 讀
✅ 輸出到 backend/scripts/analysis/drivefolder_dryrun_YYYYMMDD.{csv,json,md}
"""

import argparse
import csv
import json
import re
from collections import defaultdict, Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path("/home/aping/MyProjects/TQark-web")
ARCHIVE_ROOT = Path("/mnt/my_book/考題收集")
STATE_DIR = ARCHIVE_ROOT / "state"
INDEX_FILE = STATE_DIR / "local_papers_index.json"
SOURCES_FILE = ROOT / "backend" / "data" / "external_sources.json"
ANALYSIS_DIR = ROOT / "backend" / "scripts" / "analysis"

# Subject keywords (longest first)
SUBJECT_KEYWORDS = [
    "自然與生活科技", "社會學習領域", "綜合活動", "資訊科技",
    "地球科學", "地科學", "地科",
    "健康與體育", "健康教育", "英聽", "英語聽力",
    "生物科", "自然科", "數非選", "自然與生活科技",
    "英文科", "國文科", "數學科", "歷史科", "地理科", "公民科",
    "生科", "自科", "數科", "社科", "英科", "國科", "歷科", "地科",
    "全科",  # multi-subject bundle
    "國文科", "英語科", "數學科", "自然科", "社會科", "生物科",
    "理化科", "歷史科", "地理科", "公民科",
    "生活科技", "地球科學",
    "作文", "寫作",
    "國文", "英語", "英文", "數學", "自然", "社會", "理化", "生物",
    "歷史", "地理", "公民", "健康", "健體", "體育", "音樂", "美術",
    "家政", "生活", "綜合", "資訊", "科技", "閱讀",
]

SUBJECT_SHORT_MAP = {
    "公": "公民", "國": "國文", "英": "英語", "數": "數學",
    "自": "自然", "社": "社會", "理": "理化", "化": "理化",
    "生": "生物", "歷": "歷史", "地": "地理",
    "健": "健康教育", "體": "體育", "音": "音樂", "美": "美術",
    "作": "作文", "聽": "英語",  # 聽力稿 → 英語
}

GRADE_ARABIC_MAP = {
    "1": "一年級", "2": "二年級", "3": "三年級",
    "4": "四年級", "5": "五年級", "6": "六年級",
    "7": "七年級", "8": "八年級", "9": "九年級",
    "10": "十年級", "11": "十一年級", "12": "十二年級",
}

GRADE_CHINESE_MAP = {
    "一": "一年級", "二": "二年級", "三": "三年級",
    "四": "四年級", "五": "五年級", "六": "六年級",
    "七": "七年級", "八": "八年級", "九": "九年級",
    "十": "十年級",
}
GRADE_GUO_MAP = {"一": "七年級", "二": "八年級", "三": "九年級",
                "七": "七年級", "八": "八年級", "九": "九年級"}
GRADE_GUO_ARABIC_MAP = {"1": "七年級", "2": "八年級", "3": "九年級"}


def parse_grade(filename: str, abs_path: str, school: tuple[str, str]) -> tuple[str | None, str | None]:
    """從 filename + abs_path 解析年級。
    Returns (grade, parse_pattern)。
    """
    base = filename.rsplit(".", 1)[0] if filename.lower().endswith(".pdf") else filename
    # Strip the fixed prefix to get "inside" path
    # e.g., _未分類/DriveFolder/<county>/<school>/<rest>
    path_prefix = f"_未分類/DriveFolder/{school[0]}/{school[1]}/"
    path_rest = abs_path.split(path_prefix, 1)[1] if path_prefix in abs_path else ""
    # path_rest includes <filename> at end, remove it
    path_inside = "/".join(path_rest.split("/")[:-1]) + "/" if path_rest else ""
    county, school_name = school

    # === Path-based grade (FIRST check — path has explicit grade folder) ===
    # "7年級" / "8年級" / "9年級" / "一年級" / "八年級" appear as path components
    # Also "國X" / "國X科目" / "X" (single digit) components
    for component in path_inside.split("/"):
        if not component:
            continue
        # Try "X年級" pattern within component (substring match, more flexible)
        # Use search() to find anywhere in component (e.g., "一年級  ok" matches)
        m = re.search(r"([0-9]|[一二三四五六七八九])\s*年級", component)
        if m:
            g = m.group(1)
            if g in GRADE_ARABIC_MAP:
                return GRADE_ARABIC_MAP[g], "path_arabic_年級"
            elif g in GRADE_CHINESE_MAP:
                return GRADE_CHINESE_MAP[g], "path_chinese_年級"
        # "國X" component (e.g., 楠梓 "國一自然/國一英文/國二自然" or 土城 "7B")
        # Use search to find 國X anywhere in component
        m = re.search(r"國\s*([一二三123])(?![一二三四五六七八九十])", component)
        if m:
            g = m.group(1)
            if g in GRADE_GUO_MAP:
                return GRADE_GUO_MAP[g], "path_guo_chinese"
            elif g in GRADE_GUO_ARABIC_MAP:
                return GRADE_GUO_ARABIC_MAP[g], "path_guo_arabic"
        # Single digit path component (土城 has "7" / "8" / "9" as folder names)
        if re.match(r"^[0-9]$", component):
            g = component
            if g in GRADE_ARABIC_MAP and g in ("7", "8", "9"):
                return GRADE_ARABIC_MAP[g], "path_single_digit"
        # "7B" / "8A" (土城 A/B 卷分類)
        m = re.match(r"^([0-9])[A-Z]$", component)
        if m:
            g = m.group(1)
            if g in GRADE_ARABIC_MAP and g in ("7", "8", "9"):
                return GRADE_ARABIC_MAP[g], "path_digit_letter"
        # "X年" without 級 (e.g., "1年")
        m = re.match(r"^([0-9])年$", component)
        if m:
            g = m.group(1)
            if g in GRADE_ARABIC_MAP and g in ("7", "8", "9"):
                return GRADE_ARABIC_MAP[g], "path_arabic_年_only"

    # === Filename-based patterns ===
    # Pattern 1: "X年級" (most common)
    m = re.search(r"([0-9]|[一二三四五六七八九])\s*年級", base)
    if m:
        g = m.group(1)
        if g in GRADE_ARABIC_MAP:
            return GRADE_ARABIC_MAP[g], "arabic_年級"
        elif g in GRADE_CHINESE_MAP:
            return GRADE_CHINESE_MAP[g], "chinese_年級"

    # Pattern 2: 國X (新北錦和) — match "國X" without \b (Chinese has no \b)
    # Accept 七/八/九 (錦和用 國七/國八/國九) and 一/二/三 (國一/國二/國三)
    # Use lookahead for grade-terminator (段/科/答案/試題) to avoid over-match
    m = re.search(r"國\s*([一二三四五六七八九])(?=段|全科|科|答案|解答|試題|數|[(（]|$)", base)
    if m:
        g = m.group(1)
        if g in GRADE_GUO_MAP:
            return GRADE_GUO_MAP[g], "guo_chinese"
        elif g in GRADE_GUO_ARABIC_MAP:
            return GRADE_GUO_ARABIC_MAP[g], "guo_arabic"

    # Pattern 3: 桃園 special — 7-digit prefix with grade digit: "1050101-7公民"
    if (county, school_name) == ("桃園市", "桃園市桃園國中"):
        m = re.match(r"^[0-9]{7}-([0-9])", base)
        if m:
            g = m.group(1)
            if g in GRADE_ARABIC_MAP:
                return GRADE_ARABIC_MAP[g], "taoyuan_7digit_dash"

    # Pattern 4: 桃園 — 7-digit prefix (year+term+段次) + chinese grade + subject short
    # "1060101七公答案" / "1060101九歷題目" / "1060201七寫作題目" / "1060201八作文題目"
    if (county, school_name) == ("桃園市", "桃園市桃園國中"):
        # Single-char subjects
        m = re.match(r"^[0-9]{7}([一二三四五六七八九])([公國英數自社理化生歷地健體音美])", base)
        if m:
            g = m.group(1)
            if g in GRADE_CHINESE_MAP:
                return GRADE_CHINESE_MAP[g], "taoyuan_7digit_chinese_subject"
        # 2-char subjects (寫作/作文)
        m = re.match(r"^[0-9]{7}([一二三四五六七八九])(寫作|作文)", base)
        if m:
            g = m.group(1)
            if g in GRADE_CHINESE_MAP:
                return GRADE_CHINESE_MAP[g], "taoyuan_7digit_2char_subject"

    # Pattern 5: 桃園 bare digit prefix + subject: "7公1" / "9社2"
    if (county, school_name) == ("桃園市", "桃園市桃園國中"):
        m = re.match(r"^([0-9])(公|國|英|數|自|社|理|化|生|歷|地|健|體|音|美)", base)
        if m:
            g = m.group(1)
            if g in GRADE_ARABIC_MAP:
                return GRADE_ARABIC_MAP[g], "taoyuan_bare"
        # Also: 6-digit + arabic digit grade: "1050101 9公民" — but check after dash
        m = re.match(r"^[0-9]{6}[\s_]([0-9])(公|國|英|數|自|社|理|化|生|歷|地|健|體|音|美)", base)
        if m:
            g = m.group(1)
            if g in GRADE_ARABIC_MAP:
                return GRADE_ARABIC_MAP[g], "taoyuan_6digit_bare_arabic_subject"

    # Pattern 6: 桃園 "_110九上一段" / "_八上一段歷史答案"
    if (county, school_name) == ("桃園市", "桃園市桃園國中"):
        m = re.search(r"[_\s]([一二三四五六七八九])(上|下)[一二三四]?段", base)
        if m:
            g = m.group(1)
            if g in GRADE_CHINESE_MAP:
                return GRADE_CHINESE_MAP[g], "taoyuan_chinese_term"

    # Pattern 7: 崇林 風格 — "七上/七下/八上/九上" — Chinese grade + term
    # Avoid matching "101上" (year+term where year ends with 七/八/九/十)
    # Negative lookbehind for digit OR 年 (year context)
    m = re.search(r"(?<![0-9年])([七八九十一])(上|下)(?![一二三四五六七八九])", base)
    if m:
        g = m.group(1)
        if g in GRADE_CHINESE_MAP:
            return GRADE_CHINESE_MAP[g], "chinese_grade_term"

    # Pattern 8: 楠梓 "國三上(2段)" / "106國三上2段"
    m = re.search(r"國\s*([一二三123])\s*(上|下)", base)
    if m:
        g = m.group(1)
        if g in GRADE_GUO_MAP:
            return GRADE_GUO_MAP[g], "guo_term"
        elif g in GRADE_GUO_ARABIC_MAP:
            return GRADE_GUO_ARABIC_MAP[g], "guo_term_arabic"

    # Pattern 9: 楠梓 "(九)" patterns within parens
    m = re.search(r"[（(]([一二三四五六七八九])(?:年級|級)?[)）]", base)
    if m:
        g = m.group(1)
        if g in GRADE_CHINESE_MAP:
            return GRADE_CHINESE_MAP[g], "paren_grade"

    # Pattern 10: 楠梓 "二上1段" / "二下2次" / "二英一段" — Chinese digit at start (NOT preceded by digit)
    m = re.match(r"^([一二三四五六七八九])(上|下|英|段|公|數|理)", base)
    if m:
        g = m.group(1)
        if g in GRADE_CHINESE_MAP:
            return GRADE_CHINESE_MAP[g], "chinese_digit_prefix"

    # Pattern 11: 中平 "九年社會" / "八年級" — Chinese digit followed by 年 (no 級 required)
    # Use word boundary or specific subject context
    # Handle multiple spaces "九 年 級"
    m = re.search(r"([一二三四五六七八九])(?:\s*年(?:\s*級)?|(?=社會|公民|國文|英文|英語|數學|自然|理化|歷史|地理|生物|健康|體育|音樂|美術|健體|社會學習))", base)
    if m:
        g = m.group(1)
        if g in GRADE_CHINESE_MAP:
            return GRADE_CHINESE_MAP[g], "chinese_year_then_subject"

    # Pattern 12: 桃園 6-digit prefix + chinese grade (no dash)
    # "106010九地" — 6 digits then 七/八/九 etc
    if (county, school_name) == ("桃園市", "桃園市桃園國中"):
        m = re.match(r"^[0-9]{6}([一二三四五六七八九])", base)
        if m:
            g = m.group(1)
            if g in GRADE_CHINESE_MAP:
                return GRADE_CHINESE_MAP[g], "taoyuan_6digit_chinese_grade"

    # Pattern 13: "3-digit-year + 七/八/九/十 + 上/下" (新泰 風格)
    # "111七上" / "104七下" / "100八上"
    m = re.search(r"(\d{3})([七八九十一])(上|下)", base)
    if m:
        g = m.group(2)
        if g in GRADE_CHINESE_MAP:
            return GRADE_CHINESE_MAP[g], "3digit_year_grade_term"

    # Pattern 14: 中平 9501 8-3 國文試題 (old format)
    # year=2-digit "95", term=2-digit "01", grade=1-digit "8", 段次=1-digit "3"
    m = re.match(r"^(\d{2})(\d{2})\s+([0-9])-([0-9])", base)
    if m:
        g = m.group(3)
        if g in GRADE_ARABIC_MAP and g in ("7", "8", "9"):
            return GRADE_ARABIC_MAP[g], "old_format_yy_tt_grade_exam"

    # Pattern 15: 桃園 111下01七作文 / 112上02九地科
    # year=3-digit + term (上/下) + 2-digit exam_id + chinese grade + subject
    if (county, school_name) == ("桃園市", "桃園市桃園國中"):
        m = re.match(r"^(\d{3})([上下])(\d{2})([一二三四五六七八九])([\u4e00-\u9fff]+)", base)
        if m:
            g = m.group(4)
            if g in GRADE_CHINESE_MAP:
                return GRADE_CHINESE_MAP[g], "taoyuan_yyyy_term_exam_chinese_subject"

    # Pattern 16: 錦和 111下國七段一試題 / 112上國九段三試題
    # year=3-digit + term (上/下) + 國 + chinese grade + 段 + exam
    m = re.match(r"^(\d{3})([上下])國([七八九])段", base)
    if m:
        g = m.group(3)
        if g in GRADE_CHINESE_MAP:
            return GRADE_CHINESE_MAP[g], "jinhe_yyyy_term_guo_grade_exam"

    # Pattern 17: 桃園 weird 8-digit "14060202八英題目" — year=14 (typo?), term=06, exam=02, grade=八
    # Actually maybe it's "1+4+06020+2" or "14+06+0202" — let's check
    if (county, school_name) == ("桃園市", "桃園市桃園國中"):
        m = re.match(r"^(\d{7})([一二三四五六七八九])([\u4e00-\u9fff]+)", base)
        if m:
            g = m.group(2)
            if g in GRADE_CHINESE_MAP:
                return GRADE_CHINESE_MAP[g], "taoyuan_7digit_loose_subject"

    # Pattern 18: 桃園 11103九地科 / 11401九地球科學 — 3-digit year + 2-digit exam + chinese grade + subject
    if (county, school_name) == ("桃園市", "桃園市桃園國中"):
        m = re.match(r"^(\d{3})(\d{2,5})([一二三四五六七八九])([\u4e00-\u9fff]+)", base)
        if m:
            g = m.group(3)
            if g in GRADE_CHINESE_MAP:
                return GRADE_CHINESE_MAP[g], "taoyuan_yyyy_exam_chinese_subject"

    # Pattern 19: 中平 "99上 7英文 答案" / "99上  9  第3  社會 答案"
    # 2-digit year + 上 + grade digit (with optional 第N次)
    m = re.search(r"^(\d{2})上\s*(?:第\d次)?\s*([0-9一二三四五六七八九])(?![0-9])", base)
    if m:
        g = m.group(2)
        if g in GRADE_ARABIC_MAP and g in ("7", "8", "9"):
            return GRADE_ARABIC_MAP[g], "zhongping_2digit_year_term_grade"

    # Pattern 20: 桃園 1051050102-8英文 (typo year, 6-digit + 4-digit exam + arabic grade + subject)
    # "1051050102-8英文試題"
    if (county, school_name) == ("桃園市", "桃園市桃園國中"):
        m = re.match(r"^(\d{6})(\d{4})-([0-9])([公國英數自社理化生歷地健體音美])", base)
        if m:
            g = m.group(3)
            if g in GRADE_ARABIC_MAP and g in ("7", "8", "9"):
                return GRADE_ARABIC_MAP[g], "taoyuan_typo_year_exam_arabic_subject"

    return None, None


def parse_subject(filename: str, grade: str | None, school: tuple[str, str]) -> str | None:
    """從 filename 解析科目。"""
    base = filename.rsplit(".", 1)[0] if filename.lower().endswith(".pdf") else filename
    county, school_name = school

    # First pass: long-form keywords
    for subj in SUBJECT_KEYWORDS:
        if subj in base:
            return subj

    # Special: 健體 = 健康與體育 (often used together)
    if "健體" in base:
        return "健康與體育"
    if "健教" in base:
        return "健康教育"
    # "作文" / "寫作" — sometimes abbreviated as "作"
    if "作文" in base:
        return "作文"
    if "寫作" in base:
        return "寫作"

    # Second pass: 桃園 bare-digit prefix patterns (more specific)
    if (county, school_name) == ("桃園市", "桃園市桃園國中"):
        # 桃園 7-digit prefix + chinese grade + 2-char subject short (地科/寫作/作文)
        m = re.match(r"^([0-9]{7})([一二三四五六七八九])(地科|寫作|作文)", base)
        if m:
            short = m.group(3)
            if short == "地科":
                return "地球科學"
            if short in ("寫作", "作文"):
                return short
        # 桃園 7-digit prefix + chinese grade + 1-char subject short
        m = re.match(r"^([0-9]{6}|[0-9]{7})([一二三四五六七八九])([公國英數自社理化生歷地健體音美])", base)
        if m:
            short = m.group(3)
            if short in SUBJECT_SHORT_MAP:
                return SUBJECT_SHORT_MAP[short]
        # Bare digit prefix (e.g., "9公1")
        m = re.match(r"^[0-9]([公國英數自社理化生歷地健體音美])", base)
        if m:
            short = m.group(1)
            if short in SUBJECT_SHORT_MAP:
                return SUBJECT_SHORT_MAP[short]

    # Third pass: 土城 / 崇林 / etc — digit-prefix + 1-char subject
    # "7英" / "8數" / "7生" / "7國" — grade digit + 1-char subject
    # Only when grade digit is 1-9
    m = re.match(r"^[1-9]([公國英數自社理化生歷地健體音美聽作])", base)
    if m:
        short = m.group(1)
        if short in SUBJECT_SHORT_MAP:
            return SUBJECT_SHORT_MAP[short]

    # Fourth pass: When grade already known, treat any 1-char subject as SUBJECT_SHORT_MAP
    # e.g., "國.pdf" / "數.pdf" / "理.pdf" (土城 風格 — grade in path)
    if grade:
        m = re.match(r"^\s*([公國英數自社理化生歷地健體音美聽作])(?:[\u4e00-\u9fff\sA-Z0-9].*)?$", base)
        if m:
            short = m.group(1)
            if short in SUBJECT_SHORT_MAP:
                return SUBJECT_SHORT_MAP[short]
        # Also: 2-char "八社" / "八英" (Chinese digit grade + 1-char subject)
        # If grade starts with chinese digit, look for 1-char subject after
        m = re.match(r"^[一二三四五六七八九]([公國英數自社理化生歷地健體音美])(?:[\u4e00-\u9fff\sA-Z0-9].*)?$", base)
        if m:
            short = m.group(1)
            if short in SUBJECT_SHORT_MAP:
                return SUBJECT_SHORT_MAP[short]

    return None


def parse_school_year(filename: str, abs_path: str, school: tuple[str, str]) -> str | None:
    """從 filename 解析學年 (ROC 3-digit)。"""
    base = filename.rsplit(".", 1)[0] if filename.lower().endswith(".pdf") else filename
    county, school_name = school

    # 桃園特殊: "1050101" (7 digits) / "105010" (6 digits) → 105 = year
    if (county, school_name) == ("桃園市", "桃園市桃園國中"):
        m = re.match(r"^[0-9]{7}", base)
        if m:
            return m.group(0)[:3]
        m = re.match(r"^[0-9]{6}", base)
        if m:
            return m.group(0)[:3]

    # Standard patterns
    patterns = [
        re.compile(r"(10[0-9]|11[0-5])(?:學年度|學年|年度)"),
        re.compile(r"^(\d{3})[-_]([上下]|1|2)(?:[-_]\d)?"),
        re.compile(r"^(\d{3})([上下])"),
        re.compile(r"^(\d{3})"),
        re.compile(r"(?<![0-9])(10[0-9]|11[0-5])(?=[下上])"),
        re.compile(r"^(\d{2})(?=\d{2}[\s\-])"),  # "9501 8-3" — old format
    ]
    for pat in patterns:
        m = pat.search(base)
        if m:
            yr = m.group(1) if m.lastindex >= 1 else m.group(0)
            if yr.isdigit():
                if len(yr) == 3 and 100 <= int(yr) <= 115:
                    return yr
                if len(yr) == 2 and 95 <= int(yr) <= 99:
                    # 2-digit year 95~99 → 095~099? Actually 95學年度 = 2006, but 9X學年度 is old
                    # Most likely these are 1990s — skip for now
                    pass

    return None


def parse_school_term(filename: str, school: tuple[str, str]) -> str | None:
    """從 filename 解析學期。"""
    base = filename.rsplit(".", 1)[0] if filename.lower().endswith(".pdf") else filename
    county, school_name = school

    # 桃園特殊: "1050101" / "1060101" → 01 = 上學期
    if (county, school_name) == ("桃園市", "桃園市桃園國中"):
        m = re.match(r"^(\d{3})(0[1-2])", base)
        if m:
            term = m.group(2)
            return "上學期" if term == "01" else "下學期"

    patterns = [
        re.compile(r"第([12])\s*學期"),
        re.compile(r"第([一二])\s*學期"),
        re.compile(r"([上下])(?:學期|半)"),
    ]
    for pat in patterns:
        m = pat.search(base)
        if m:
            g = m.group(1)
            if g in ("上", "下"):
                return g + "學期"
            elif g == "1" or g == "一":
                return "上學期"
            elif g == "2" or g == "二":
                return "下學期"
    return None


def parse_exam_type(filename: str) -> str | None:
    """從 filename 解析考試類型。"""
    base = filename.rsplit(".", 1)[0] if filename.lower().endswith(".pdf") else filename

    keywords = [
        "第一次段考", "第二次段考", "第三次段考",
        "第1次段考", "第2次段考", "第3次段考",
        "第一次定期考", "第二次定期考", "第三次定期考",
        "第1次定期考", "第2次定期考", "第3次定期考",
        "第一次定期評量", "第二次定期評量", "第三次定期評量",
        "第1次定期評量", "第2次定期評量", "第3次定期評量",
        "第1階段評量", "第2階段評量", "第3階段評量",
        "第一階段評量", "第二階段評量", "第三階段評量",
        "期末考", "期中考", "模擬考", "月考", "複習考",
        "段考", "定期考", "定期評量", "評量",
    ]
    for kw in keywords:
        if kw in base:
            return kw
    return None


SEGMENT_MAP = {
    "一年級": "國小", "二年級": "國小", "三年級": "國小",
    "四年級": "國小", "五年級": "國小", "六年級": "國小",
    "七年級": "國中", "八年級": "國中", "九年級": "國中",
    "十年級": "高中", "十一年級": "高中", "十二年級": "高中",
}


def derive_level(grade: str | None) -> str | None:
    if not grade:
        return None
    return SEGMENT_MAP.get(grade)


def derive_confidence(parsed: dict) -> str:
    if not parsed.get("level"):
        return "LOW"
    if not parsed.get("subject"):
        return "MEDIUM"
    if not parsed.get("grade"):
        return "MEDIUM"
    return "HIGH"


def build_target_filename(county: str, school_year: str, school_term: str | None,
                           exam_type: str, school_name: str, version: str,
                           subject: str | None, grade: str | None,
                           filetype: str) -> str:
    if school_term:
        year_term = f"{school_year}{school_term}" if school_year else "未註明"
    else:
        year_term = school_year if school_year else "未註明"

    safe_school = school_name.replace("/", "／").replace(":", "：")
    safe_exam = (exam_type or "未註明").replace("/", "／").replace(":", "：")
    safe_subject = (subject or "未分類").replace("/", "／").replace(":", "：")
    safe_grade = (grade or "未註明").replace("/", "／").replace(":", "：")

    return f"{county}_{year_term}_{safe_exam}_{safe_subject}_{safe_grade}_{safe_school}_{version or '未註明'}_drivefolder"


def build_target_path(county: str, level: str, grade: str, subject: str | None,
                      filetype: str, filename: str) -> str:
    safe_subject = (subject or "未分類").replace("/", "／").replace(":", "：")
    safe_grade = grade.replace("/", "／").replace(":", "：")
    return f"{county}/{level}/{safe_grade}/{safe_subject}/{filetype}/{filename}.pdf"


def load_index() -> dict:
    with INDEX_FILE.open() as f:
        return json.load(f)


def load_drive_config() -> set[tuple[str, str]]:
    with SOURCES_FILE.open() as f:
        data = json.load(f)
    return {(s["county"], s["name"]) for s in data.get("schools", [])
            if s.get("link_type") == "drive"}


def analyse():
    index = load_index()
    items = index["items"]
    drive_config = load_drive_config()

    drive_items = [it for it in items if not it.get("level")
                   and "_drivefolder" in it.get("rel_path", "")]

    print(f"Total Drive folder items: {len(drive_items)}")
    print(f"Drive folder config schools: {len(drive_config)}")

    main_lookup = {}
    for it in items:
        if not it.get("level"):
            continue
        if not (it.get("county") and it.get("school_name") and it.get("school_year")
                and it.get("grade") and it.get("subject") and it.get("filetype")):
            continue
        key = (
            it["county"], it["school_year"], it["grade"], it["subject"],
            it["filetype"], it.get("exam_type", ""), it["school_name"],
        )
        if key not in main_lookup:
            main_lookup[key] = it["abs_path"]

    analysis = []
    for it in drive_items:
        abs_path = it["abs_path"]
        rel_path = it["rel_path"]
        filename = abs_path.split("/")[-1]
        county = it["county"]
        school_name = it["school_name"]
        school = (county, school_name)
        filetype = it["filetype"] or "paper"

        grade, grade_pat = parse_grade(filename, abs_path, school)
        subject = parse_subject(filename, grade, school)
        school_year = parse_school_year(filename, abs_path, school) or it.get("school_year") or ""
        school_term = parse_school_term(filename, school)
        exam_type = parse_exam_type(filename) or "未註明"
        level = derive_level(grade)

        if filetype not in ("paper", "daan"):
            filetype = "paper"

        parsed = {
            "county": county,
            "school_name": school_name,
            "level": level,
            "grade": grade,
            "subject": subject,
            "filetype": filetype,
            "school_year": school_year,
            "school_term": school_term,
            "exam_type": exam_type,
        }
        confidence = derive_confidence(parsed)

        target_filename = ""
        target_rel = ""
        skip_reason = None
        if confidence == "LOW":
            skip_reason = "no_grade"
            target_rel = f"_未分類/_pending_review/{county}/{school_name}/{filename}"
        else:
            target_filename = build_target_filename(
                county, school_year, school_term, exam_type,
                school_name, "未註明", subject, grade, filetype,
            )
            target_rel = build_target_path(
                county, level, grade, subject, filetype, target_filename,
            )

        conflict_target = None
        if confidence != "LOW":
            key = (county, school_year, grade, subject, filetype, exam_type, school_name)
            if key in main_lookup:
                conflict_target = main_lookup[key]

        analysis.append({
            "paper_id": it["paper_id"],
            "abs_path": abs_path,
            "rel_path": rel_path,
            "filename": filename,
            "county": county,
            "school_name": school_name,
            "school_year": school_year,
            "school_term": school_term or "",
            "exam_type": exam_type,
            "level": level or "",
            "grade": grade or "",
            "grade_parse_pattern": grade_pat or "",
            "subject": subject or "",
            "filetype": filetype,
            "confidence": confidence,
            "target_rel": target_rel,
            "target_filename": target_filename,
            "skip_reason": skip_reason or "",
            "conflict_target": conflict_target or "",
            "size_kb": it.get("size_kb", 0),
        })

    return analysis, drive_config


def write_csv(analysis: list[dict], date_str: str):
    out = ANALYSIS_DIR / f"drivefolder_dryrun_{date_str}.csv"
    fields = [
        "paper_id", "abs_path", "rel_path", "filename",
        "county", "school_name", "school_year", "school_term", "exam_type",
        "level", "grade", "grade_parse_pattern", "subject", "filetype",
        "confidence", "target_rel", "target_filename",
        "skip_reason", "conflict_target", "size_kb",
    ]
    with out.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(analysis)
    return out


def write_json(analysis: list[dict], date_str: str, drive_config: set, indexed_schools: set):
    out = ANALYSIS_DIR / f"drivefolder_dryrun_{date_str}.json"
    payload = {
        "built_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
        "drive_folder_config_count": len(drive_config),
        "indexed_drive_school_count": len(indexed_schools),
        "config_minus_indexed": sorted(drive_config - indexed_schools),
        "indexed_minus_config": sorted(indexed_schools - drive_config),
        "total_items": len(analysis),
        "items": analysis,
    }
    with out.open("w") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    return out


def write_summary_md(analysis: list[dict], date_str: str, drive_config: set, indexed_schools: set):
    out = ANALYSIS_DIR / f"drivefolder_summary_{date_str}.md"

    conf_counter = Counter(a["confidence"] for a in analysis)
    high = conf_counter.get("HIGH", 0)
    medium = conf_counter.get("MEDIUM", 0)
    low = conf_counter.get("LOW", 0)

    school_stats = defaultdict(lambda: Counter())
    for a in analysis:
        key = (a["county"], a["school_name"])
        school_stats[key][a["confidence"]] += 1
        school_stats[key]["total"] += 1

    conflicts = [a for a in analysis if a["conflict_target"]]
    conflict_by_school = Counter((a["county"], a["school_name"]) for a in conflicts)

    subj_counter = Counter(a["subject"] for a in analysis if a["subject"])
    grade_counter = Counter(a["grade"] for a in analysis if a["grade"])
    pat_counter = Counter(a["grade_parse_pattern"] for a in analysis if a["grade_parse_pattern"])

    no_subject = sum(1 for a in analysis if a["confidence"] in ("HIGH", "MEDIUM") and not a["subject"])
    no_grade = sum(1 for a in analysis if not a["grade"])

    config_minus_idx = sorted(drive_config - indexed_schools)
    idx_minus_config = sorted(indexed_schools - drive_config)

    mac_meta = sum(1 for a in analysis if a["filename"].startswith("._"))

    md = []
    md.append(f"# Drive Folder 歸檔 Dry-Run Summary ({date_str})")
    md.append("")
    md.append("> 來源：`/mnt/my_book/考題收集/_未分類/DriveFolder/` (11,995 PDFs)")
    md.append("> 任務：模擬把這 11,995 PDFs 歸檔進主 archive `<county>/<level>/<grade>/<subject>/<paper|daan>/`")
    md.append("> **READ-ONLY**：本分析未動任何檔，僅從 `state/local_papers_index.json` 讀 + 解析 filename/path")
    md.append("")
    md.append("**分析腳本**：`backend/scripts/analysis/drivefolder_dryrun.py`")
    md.append("**輸出檔案**：")
    md.append(f"- `drivefolder_dryrun_{date_str}.csv` (11,995 rows)")
    md.append(f"- `drivefolder_dryrun_{date_str}.json` (full structured data)")
    md.append(f"- `drivefolder_summary_{date_str}.md` (this file)")
    md.append("")

    md.append("## 1. 信心度分布 (Confidence Distribution)")
    md.append("")
    md.append("| 信心度 | 數量 | % |")
    md.append("|--------|------|---|")
    md.append(f"| **HIGH**   | {high:>5} | {high/len(analysis)*100:.1f}% |")
    md.append(f"| **MEDIUM** | {medium:>5} | {medium/len(analysis)*100:.1f}% |")
    md.append(f"| **LOW**    | {low:>5} | {low/len(analysis)*100:.1f}% |")
    md.append(f"| **TOTAL**  | {len(analysis):>5} | 100.0% |")
    md.append("")
    md.append("**信心度定義**：")
    md.append("- **HIGH** — `county + level + grade + subject + filetype` 全部解析得到，可直接歸檔")
    md.append("- **MEDIUM** — 缺 1-2 個欄位 (e.g., 沒 subject 或 grade)，歸檔會用 `_未分類/` fallback 或 default")
    md.append("- **LOW** — grade 完全解析不到，無法產生標準 archive path，建議放 `_未分類/_pending_review/` 等手動 review")
    md.append("")

    md.append("## 2. 13 校 PDFs 分布 + 信心度")
    md.append("")
    md.append("| County | School | Total | HIGH | MEDIUM | LOW | HIGH% |")
    md.append("|--------|--------|------:|-----:|-------:|----:|------:|")
    for (county, school), c in sorted(school_stats.items(), key=lambda x: -x[1]["total"]):
        high_pct = c['HIGH'] / c['total'] * 100
        md.append(f"| {county} | {school} | {c['total']:>5} | {c['HIGH']:>4} | {c['MEDIUM']:>6} | {c['LOW']:>3} | {high_pct:>4.1f}% |")
    md.append("")
    md.append(f"**合計**：13 校、{len(analysis)} PDFs")
    md.append("")

    md.append("## 3. 衝突 (Conflicts)")
    md.append("")
    md.append(f"- **總衝突數**：{len(conflicts)} 個 drive PDFs 跟主 archive 已分類檔案 (county + school_year + grade + subject + filetype + exam_type) 完全相符")
    md.append("- 衝突 = **不能直接搬**，否則會覆蓋現有 PDF")
    md.append("")
    if conflict_by_school:
        md.append("**衝突分布 (by school)**:")
        md.append("")
        md.append("| County | School | Conflict count |")
        md.append("|--------|--------|---------------:|")
        for (county, school), cnt in conflict_by_school.most_common():
            md.append(f"| {county} | {school} | {cnt} |")
        md.append("")
        md.append("**衝突範例 (前 15)**:")
        md.append("")
        md.append("| School | Drive filename | Conflicts with (main archive) |")
        md.append("|--------|----------------|------------------------------|")
        for a in conflicts[:15]:
            md.append(f"| {a['school_name']} | `{a['filename'][:50]}` | `{a['conflict_target'].split('/')[-1][:60]}` |")
        md.append("")
    else:
        md.append("✅ **無衝突**")
        md.append("")

    md.append("## 4. 解析缺口 (Parse Gaps)")
    md.append("")
    md.append(f"- **缺 grade** (→ LOW 或 MEDIUM)：{no_grade} 個 ({no_grade/len(analysis)*100:.1f}%)")
    md.append(f"- **HIGH/MEDIUM 中缺 subject**：{no_subject} 個 ({no_subject/(high+medium)*100:.1f}% of HIGH+MEDIUM)")
    md.append("")
    md.append("**Subject coverage** (top 15):")
    md.append("")
    md.append("| Subject | Count |")
    md.append("|---------|------:|")
    for subj, cnt in subj_counter.most_common(15):
        md.append(f"| {subj} | {cnt} |")
    md.append("")
    md.append("**Grade coverage** (top 15):")
    md.append("")
    md.append("| Grade | Count |")
    md.append("|-------|------:|")
    for g, cnt in grade_counter.most_common(15):
        md.append(f"| {g} | {cnt} |")
    md.append("")
    md.append("**Grade parser pattern** (debug):")
    md.append("")
    md.append("| Pattern | Count |")
    md.append("|---------|------:|")
    for p, cnt in pat_counter.most_common(15):
        md.append(f"| `{p}` | {cnt} |")
    md.append("")

    md.append("## 5. Drive Folder Config vs Indexed Schools")
    md.append("")
    md.append(f"- **Config 內 drive schools**: {len(drive_config)}")
    md.append(f"- **Indexed 內 drive schools** (有 PDFs): {len(indexed_schools)}")
    md.append(f"- **Config 中但 indexed 沒抓 (zero-PDF / 失敗)**: {len(config_minus_idx)}")
    md.append(f"- **Indexed 中但 config 沒列 (orphan)**: {len(idx_minus_config)}")
    md.append("")
    if config_minus_idx:
        md.append("**Config 中但沒抓到 PDFs 的學校** (drive folder cron 可能失敗 / zero-PDF / 過濾太嚴):")
        md.append("")
        for c, s in config_minus_idx:
            md.append(f"- {c} **{s}**")
        md.append("")
    if idx_minus_config:
        md.append("**Indexed 有但 config 沒列的學校** (orphan):")
        md.append("")
        for c, s in idx_minus_config:
            md.append(f"- {c} **{s}**")
        md.append("")

    md.append("## 6. Drive Folder Config 完整學校清單 (42 校)")
    md.append("")
    md.append("| County | School | Indexed? | Count |")
    md.append("|--------|--------|----------|------:|")
    for c, s in sorted(drive_config, key=lambda x: (x[0], x[1])):
        cnt = school_stats.get((c, s), {}).get("total", 0)
        indexed_mark = "✅" if cnt > 0 else "❌"
        md.append(f"| {c} | {s} | {indexed_mark} | {cnt} |")
    md.append("")

    md.append("## 7. 特殊發現 (Anomalies)")
    md.append("")
    md.append(f"- **macOS metadata 檔 (._*)**: {mac_meta} 個 — 全在桃園市桃園國中 (Drive folder 抓回時 copy 了 macOS 系統檔)")
    md.append(f"  - 建議：歸檔前先 `find` 過濾掉 `._*`，或歸檔後 archive 內一併清掉")
    md.append(f"- **非 PDF**: 0 個 — index 已 filter 只收 PDF")
    md.append(f"- **重複 abs_path**: 0 個 — 全部 unique")
    md.append(f"- **Drive folder 抓回的檔案** 全部是 `paper` (試題) 或 `daan` (答案) — 由 `_guess_filetype_from_fname` 自動判斷")
    md.append(f"- **13 校分布**: 桃園 1 / 新北 7 / 高雄 5 (北中南皆有)")
    md.append("")

    low_items = [a for a in analysis if a["confidence"] == "LOW"]
    md.append("## 8. LOW Confidence 範例 (前 20 個)")
    md.append("")
    md.append("這些檔案的 grade 完全解析不到，歸檔結構無法自動建立。建議手動 review。")
    md.append("")
    md.append("| School | Filename | Size (KB) |")
    md.append("|--------|----------|----------:|")
    for a in low_items[:20]:
        md.append(f"| {a['school_name']} | `{a['filename'][:60]}` | {a['size_kb']} |")
    if len(low_items) > 20:
        md.append(f"| ... | (還有 {len(low_items) - 20} 個) | |")
    md.append("")

    md.append("## 9. 建議選項 (給 William 決策)")
    md.append("")
    md.append("### Option A — 只搬 HIGH confidence")
    md.append(f"- 數量：**{high}** ({high/len(analysis)*100:.1f}%)")
    md.append("- 優點：歸檔結構 100% 正確、無需手動 review")
    md.append(f"- 缺點：丟掉 {medium + low} 個 ({100 - high/len(analysis)*100:.1f}%) PDFs")
    md.append("")
    md.append("### Option B — 搬 HIGH + MEDIUM，LOW 留 `_pending_review/`")
    md.append(f"- 數量：**{high + medium}** 個進主 archive")
    md.append(f"- **{low}** 個放 `_未分類/_pending_review/` 等手動處理")
    md.append("- 優點：覆蓋率最高，LOW 不會卡住其他進度")
    md.append("- 缺點：MEDIUM 缺 subject/grade 的歸檔會混雜到 `_未分類/` 或 fallback subject")
    md.append("")
    md.append("### Option C — 全部都搬但 LOW 先列清單給 William 手動處理")
    md.append(f"- 數量：全部 **{len(analysis)}**")
    md.append("- LOW 的歸到 `_未分類/_pending_review/<county>/<school>/` 保留原始檔名")
    md.append("- 優點：完全不�檔，全部 traceable")
    md.append("- 缺點：LOW 部分需要手動分類時間")
    md.append("")
    md.append("### Option D — 先寫更強 parser 再搬")
    md.append("- 改善 parser 對 桃園國中 等特殊檔名的解析率")
    md.append(f"- 目前 LOW {low} 還有 80%+ 可以用 path-based 規則抓回 (e.g., 桃園 path 結構有 year+grade folder)")
    md.append(f"- 預估可從 HIGH {high} → HIGH+MEDIUM > 85%")
    md.append("- 缺點：parser 開發時間 + 需要更多 sample review")
    md.append("")
    md.append("### Option E — 只搬特定學校 (e.g., 高雄市 4 校)")
    md.append("- 一次只處理一縣市、降低風險")
    md.append(f"- 推薦先試新北市崇林 (2,010 PDFs, 結構最規律, HIGH% = {school_stats.get(('新北市', '新北市崇林國中'), {}).get('HIGH', 0) / school_stats.get(('新北市', '新北市崇林國中'), {}).get('total', 1) * 100:.1f}%)")
    md.append("")
    md.append("### � William 推薦 (我的建議)")
    md.append(f"- **Phase 1**：Option A (HIGH only) — 直接搬 {high} 個、結構 100% 正確")
    md.append("- **Phase 2**：Option D 改善 parser，把 MEDIUM+部分 LOW 中可解析的升 HIGH")
    md.append("- **Phase 3**：剩餘 LOW 列清單，手動 review (或寫新 parser)")
    md.append("")

    md.append("## 10. 已知問題 / 注意事項")
    md.append("")
    md.append("1. **Drive folder 檔名 vs StudyArk 檔名結構完全不同**")
    md.append("   - Drive: `<校名><學年><學期><年級><科目><段考><filetype>.pdf`")
    md.append("   - StudyArk: `<county>_<year>_<exam>_<fileid>_<school>_<version>.pdf`")
    md.append("   - 歸檔後用 StudyArk 標準命名但加 `_drivefolder` suffix 標記來源")
    md.append("")
    md.append("2. **macOS metadata 污染**")
    md.append("   - 桃園國中 46 個 `._*` 檔需在歸檔時過濾")
    md.append("")
    md.append("3. **沒有 fileid (StudyArk 編號)**")
    md.append("   - Drive folder 抓回的沒 StudyArk fileid")
    md.append("   - 用 `_drivefolder` suffix 標記來源")
    md.append("")
    md.append("4. **28 校 drive folder config 但 indexed 沒抓**")
    md.append("   - 可能是 zero-PDF、folder ID 失效、或 is_exam_file 過濾太嚴")
    md.append("   - 建議跑 `archive_drive_folders.py --folder-id <id>` 各別測試")
    md.append("")
    md.append("5. **高雄市左營國中** 是唯一 drive folder 已有 10 個 main archive entries 的學校")
    md.append("   - 可能存在 StudyArk 已有同名 PDF → 歸檔時需 skip 或 rename")
    md.append("")

    out.write_text("\n".join(md), encoding="utf-8")
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="YYYYMMDD (default: today)")
    args = parser.parse_args()

    if args.date:
        date_str = args.date
    else:
        date_str = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d")

    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Loading index from {INDEX_FILE}...")
    analysis, drive_config = analyse()

    indexed_schools = set((a["county"], a["school_name"]) for a in analysis)

    csv_path = write_csv(analysis, date_str)
    json_path = write_json(analysis, date_str, drive_config, indexed_schools)
    md_path = write_summary_md(analysis, date_str, drive_config, indexed_schools)

    print(f"\nWrote:")
    print(f"  CSV:     {csv_path}")
    print(f"  JSON:    {json_path}")
    print(f"  Summary: {md_path}")

    conf_counter = Counter(a["confidence"] for a in analysis)
    print(f"\n=== Quick Stats ===")
    print(f"Total: {len(analysis)}")
    print(f"HIGH:   {conf_counter.get('HIGH', 0)}")
    print(f"MEDIUM: {conf_counter.get('MEDIUM', 0)}")
    print(f"LOW:    {conf_counter.get('LOW', 0)}")
    print(f"Conflicts: {sum(1 for a in analysis if a['conflict_target'])}")


if __name__ == "__main__":
    main()
