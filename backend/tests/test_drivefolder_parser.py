"""
Tests for drivefolder_dryrun.py parser functions (Phase 2 — 學期+段考維度)

8/25: 修 parse_school_term 太嚴格問題 — 崇林 1,131 個 source filename
      用「101下自然七年級第一次段考」這種 <3digit><上/下> 開頭但「下」後面沒「學期」字
      parser 抓不到 → 全部 hash 到同一個 target_rel → 不同段考/不同學期的考卷 merge
測試重點:
- parse_school_term 對崇林風格 filename 回正確 (上/下學期)
- parse_school_year 對「崇林<3digit>」prefix filename 回正確學年
- build_target_filename 加 (semester, exam) 後 target unique
"""

import sys
from pathlib import Path

import pytest

# 把 scripts/analysis 加到 path
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "analysis"))

from drivefolder_dryrun import (
    parse_school_term,
    parse_school_year,
    build_target_filename,
)


# === Fixtures ===

CHONGLIN = ("新北市", "新北市崇林國中")


# === parse_school_term tests ===

def test_parse_school_term_chonglin_3digit_term_no_xueqi():
    """崇林風格: '101下自然七年級第一次段考.pdf' → '下學期'

    這是 1,131 個 size-mismatch 的典型 sample — 「下」後面沒「學期」字
    原本 regex 只 match '上學期' / '上半' / '下半'，這個會 parse 失敗回 None。
    """
    assert parse_school_term("101下自然七年級第一次段考.pdf", CHONGLIN) == "下學期"


def test_parse_school_term_chonglin_chonglin_prefix():
    """崇林 '崇林102下自然第一次段考.pdf' → '下學期'

    filename 開頭是「崇林」不是數字 → parse_school_year 也要處理
    """
    assert parse_school_term("崇林102下自然第一次段考.pdf", CHONGLIN) == "下學期"


def test_parse_school_term_chonglin_shang():
    """崇林 '111上七年級英語.pdf' → '上學期'"""
    assert parse_school_term("111上七年級英語.pdf", CHONGLIN) == "上學期"


def test_parse_school_term_chonglin_dash_format():
    """崇林 '101-2-八年級第三次段考.pdf' → '下學期'

    -2 在崇林 = 第二學期 = 下學期
    """
    assert parse_school_term("101-2-八年級第三次段考.pdf", CHONGLIN) == "下學期"


def test_parse_school_term_legacy_xueqi_still_works():
    """回歸: 原本能 parse 的 'XX學年下學期' 還是要能 parse"""
    assert parse_school_term("101學年下學期七年級自然.pdf", CHONGLIN) == "下學期"
    assert parse_school_term("98學年上學期七年級自然.pdf", CHONGLIN) == "上學期"


# === parse_school_year tests ===

def test_parse_school_year_chonglin_chonglin_prefix():
    """崇林 '崇林102下自然第一次段考.pdf' → '102'

    filename 開頭是「崇林」不是數字 → 原本 regex `^(\\d{3})` 不 match
    """
    abs_path = "/mnt/_未分類/DriveFolder/新北市/新北市崇林國中/102學年下學期/第一次段考/七年級/崇林102下自然第一次段考.pdf"
    assert parse_school_year("崇林102下自然第一次段考.pdf", abs_path, CHONGLIN) == "102"


def test_parse_school_year_chonglin_3digit_prefix():
    """崇林 '101下自然七年級第一次段考.pdf' → '101'"""
    abs_path = "/mnt/_未分類/DriveFolder/新北市/新北市崇林國中/101學年下學期/第一次段考/七年級/101下自然七年級第一次段考.pdf"
    assert parse_school_year("101下自然七年級第一次段考.pdf", abs_path, CHONGLIN) == "101"


def test_parse_school_year_chonglin_dash_format():
    """崇林 '101-2-八年級第三次段考.pdf' → '101'"""
    abs_path = "/mnt/_未分類/DriveFolder/新北市/新北市崇林國中/101學年下學期/第三次段考/八年級/101-2-八年級第三次段考.pdf"
    assert parse_school_year("101-2-八年級第三次段考.pdf", abs_path, CHONGLIN) == "101"


# === build_target_filename tests (確保學期+段考進入 target) ===

def test_build_target_filename_chonglin_unique_per_semester_exam():
    """build_target_filename 對同校同年同科同年級、不同 (學期, 段考) 應該產不同 target

    這是 Option A 的核心 — 不同段考的考卷不該 merge 到同一個 target。
    """
    # 同樣是 101 學年、崇林、數學、七年級、paper — 但不同 (上/下, 第一次/第二次)
    fn_up_1st = build_target_filename(
        county="新北市", school_year="101", school_term="上學期",
        exam_type="第一次段考", school_name="新北市崇林國中",
        version="未註明", subject="數學", grade="七年級", filetype="paper",
    )
    fn_up_2nd = build_target_filename(
        county="新北市", school_year="101", school_term="上學期",
        exam_type="第二次段考", school_name="新北市崇林國中",
        version="未註明", subject="數學", grade="七年級", filetype="paper",
    )
    fn_down_1st = build_target_filename(
        county="新北市", school_year="101", school_term="下學期",
        exam_type="第一次段考", school_name="新北市崇林國中",
        version="未註明", subject="數學", grade="七年級", filetype="paper",
    )
    fn_no_term = build_target_filename(
        county="新北市", school_year="101", school_term=None,
        exam_type="第一次段考", school_name="新北市崇林國中",
        version="未註明", subject="數學", grade="七年級", filetype="paper",
    )
    # 三個 (上/下 x 第一次/第二次) 都應該產不同 target name
    targets = {fn_up_1st, fn_up_2nd, fn_down_1st}
    assert len(targets) == 3, f"學期/段考應該產不同 target, got: {targets}"
    # school_term=None 的也要跟有 term 的不同 (否則還是 merge)
    assert fn_no_term not in targets
    # 確認 term 有塞進 target name
    assert "上學期" in fn_up_1st
    assert "下學期" in fn_down_1st
    # 確認 exam 有塞進 target name (目前 parser 有時 parse 出「第一次段考」、有時 None)
    assert "第一次段考" in fn_up_1st
    assert "第二次段考" in fn_up_2nd


# === 8/25 第二輪: 崇林 <year><sem>-<exam> 編碼 ===

def test_parse_school_term_chonglin_digit_semester_encoding():
    """崇林 '<year><sem>-<exam>' 編碼: '1082-2-7社會.pdf' → '下學期'

    崇林編碼規則:
    - <3digit> = 學年
    - <1|2> = 學期 (1=上, 2=下)
    - -<digit> = 段考次
    - e.g., '1082-2-7社會' = 108學年下學期第二次段考七年級社會
    """
    assert parse_school_term("1082-2-7社會.pdf", CHONGLIN) == "下學期"


def test_parse_school_term_chonglin_digit_semester_upper():
    """崇林 '1071-3七年級自然試卷.pdf' → '上學期'"""
    assert parse_school_term("1071-3七年級自然試卷.pdf", CHONGLIN) == "上學期"


def test_parse_school_year_chonglin_digit_semester_encoding():
    """崇林 '1082-2-7社會.pdf' → '108'"""
    abs_path = "/mnt/_未分類/DriveFolder/新北市/新北市崇林國中/108學年下學期/第二次段考/七年級/1082-2-7社會.pdf"
    assert parse_school_year("1082-2-7社會.pdf", abs_path, CHONGLIN) == "108"


# === parse_exam_type tests ===

from drivefolder_dryrun import parse_exam_type


def test_parse_exam_type_chonglin_digit_encoding():
    """崇林 '1082-2-7社會.pdf' → '第二次段考'

    編碼規則: 第 6 個字元 (學年第 4 字元是學期碼, 第 5 字元是 '-', 第 6 字元是段考次)
    """
    assert parse_exam_type("1082-2-7社會.pdf") == "第二次段考"


def test_parse_exam_type_chonglin_first_exam():
    """崇林 '1071-1八年級英文試題.pdf' → '第一次段考'"""
    assert parse_exam_type("1071-1八年級英文試題.pdf") == "第一次段考"


def test_parse_exam_type_chonglin_third_exam():
    """崇林 '1072-3七年級自然試卷.pdf' → '第三次段考'"""
    assert parse_exam_type("1072-3七年級自然試卷.pdf") == "第三次段考"
