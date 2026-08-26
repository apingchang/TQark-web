"""
Tests for migrate_drivefolder_metadata.parse_filename (2026-08-26 新)

Stage 1: 從 _drivefolder filename 抽出欄位塞進 DB
Filename format (apply v3 產生的):
  7 parts: <county>_<yr+term>_<exam>_<subject>_<grade>_<school>_<version>_drivefolder.pdf
  6 parts: <county>_<yr+term>_<subject>_<grade>_<school>_<version>_drivefolder.pdf (沒 exam)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from migrate_drivefolder_metadata import parse_filename


def test_7_parts_with_term_exam():
    """完整 7-part: county + year+term + exam + subject + grade + school + version"""
    p = parse_filename("新北市_101下學期_第一次段考_自然_七年級_新北市崇林國中_未註明_drivefolder.pdf")
    assert p['school_year'] == '101'
    assert p['school_term'] == '下學期'
    assert p['exam_type'] == '第一次段考'
    assert p['subject'] == '自然'
    assert p['grade'] == '七年級'
    assert p['school'] == '新北市崇林國中'


def test_7_parts_year_no_term():
    """year 但沒 term"""
    p = parse_filename("台南市_111_段考_公民_一年級_台南市復興國中_未註明_drivefolder.pdf")
    assert p['school_year'] == '111'
    assert 'school_term' not in p
    assert p['exam_type'] == '段考'


def test_7_parts_all_weizhu():
    """全未註明 → return subject/grade/school only"""
    p = parse_filename("台南市_未註明_未註明_公民_一年級_台南市復興國中_未註明_drivefolder.pdf")
    assert 'school_year' not in p
    assert 'school_term' not in p
    assert 'exam_type' not in p
    assert 'version' not in p
    assert p['subject'] == '公民'
    assert p['grade'] == '一年級'
    assert p['school'] == '台南市復興國中'


def test_7_parts_only_exam():
    """year 未註明但 exam 有"""
    p = parse_filename("台南市_未註明_第一次定期考_公民_一年級_台南市復興國中_未註明_drivefolder.pdf")
    assert 'school_year' not in p
    assert 'school_term' not in p
    assert p['exam_type'] == '第一次定期考'


def test_6_parts_no_exam():
    """6-part: 沒 exam, [county, yr+term, subject, grade, school, version]"""
    p = parse_filename("台南市_111上學期_公民_一年級_台南市復興國中_康軒_drivefolder.pdf")
    assert p['school_year'] == '111'
    assert p['school_term'] == '上學期'
    assert 'exam_type' not in p
    assert p['subject'] == '公民'
    assert p['version'] == '康軒'


def test_6_parts_year_no_term():
    """6-part with year only (no exam, no version)"""
    p = parse_filename("台南市_111_公民_一年級_台南市復興國中_康軒_drivefolder.pdf")
    assert p['school_year'] == '111'
    assert 'school_term' not in p
    assert p['version'] == '康軒'


def test_invalid_filename():
    """不是 _drivefolder.pdf → return {}"""
    p = parse_filename("regular_file.pdf")
    assert p == {}
    p = parse_filename("not_a_drivefolder_file.pdf")
    assert p == {}


def test_wrong_parts_count():
    """parts 數量不對 → return {}"""
    p = parse_filename("county_yr_subject_drivefolder.pdf")  # only 4 parts
    assert p == {}
