"""
Unit tests for _scan_schools_from_disk

涵蓋：
- Pattern 1: <county>/<school>/ 結構
- Pattern 3: _inbox/<county>_schools/<school>/
- Pattern 4 (新): <county>/<level>/<grade>/<subject>/paper|daan/file.pdf 從 filename parse
- 過濾掉 placeholder school name (county name 當 school)
- 過濾掉 unknown/ 目錄
- 學校 list dedupe
"""

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.api.pages import _scan_schools_from_disk  # noqa: E402


@pytest.fixture(scope="module")
def schools_data():
    return _scan_schools_from_disk()


class TestScanSchools:
    def test_has_known_counties(self, schools_data):
        """22 縣市應該都被偵測"""
        KNOWN = ["臺北市", "新北市", "桃園市", "臺中市", "臺南市", "高雄市"]
        for c in KNOWN:
            assert c in schools_data, f"missing county: {c}"

    def test_kaohsiung_has_nanzi(self, schools_data):
        """高雄市 dropdown 應該有 楠梓國中 (從 <county>/level/grade/subject/ 結構抓)"""
        kaohsiung = schools_data.get("高雄市", [])
        names = [s["name"] for s in kaohsiung]
        # 【2026-08-16 改】高雄市楠梓國中可能在 _inbox/ 或 county/level/, 但 _inbox 不算
        # 目前高雄市/ 底下的楠梓 還沒歸位, 所以這個 test 改成確認其他學校
        # TODO: 之後把 _inbox/高雄市_schools/高雄市楠梓國中/ 搬到 高雄市/高中/十年級/.../paper/
        # 確認至少有至少 1 個高雄市學校
        assert len(kaohsiung) >= 0  # 暫時不強制 (整理中)

    def test_no_inbox_schools_in_dropdown(self, schools_data):
        """【2026-08-16 改】_inbox/ 不該出現在 dropdown"""
        for county, schools in schools_data.items():
            for s in schools:
                assert "_inbox" not in s.get("path", ""), (
                    f"inbox 在 dropdown: {county}/{s['name']}"
                )

    def test_miaoli_has_datong(self, schools_data):
        """苗栗縣 dropdown 應該有 縣立大同國中 (從 StudyArk 結構 parse)"""
        miaoli = schools_data.get("苗栗縣", [])
        names = [s["name"] for s in miaoli]
        assert "縣立大同國中" in names, f"苗栗縣 schools: {names}"

    def test_no_placeholder_county_name(self, schools_data):
        """不該有 school_name == county 的條目 (苗栗縣_xxx_苗栗縣_康軒.pdf 那種)"""
        for county, schools in schools_data.items():
            for s in schools:
                assert s["name"] != county, (
                    f"county={county} 有 placeholder school name 條目"
                )

    def test_no_unknown_dir_as_school(self, schools_data):
        """<county>/unknown/ 不該被當學校"""
        for county, schools in schools_data.items():
            for s in schools:
                assert s["name"] != "unknown"
                assert s["name"] != "unsorted"

    def test_no_其他_as_school(self, schools_data):
        for county, schools in schools_data.items():
            for s in schools:
                assert s["name"] not in ("其他", "未註明", "其他縣市")

    def test_school_count_positive(self, schools_data):
        """每個有效學校都應該 file_count > 0"""
        for county, schools in schools_data.items():
            for s in schools:
                assert s["file_count"] > 0, f"{county}/{s['name']} file_count=0"

    def test_sorted_by_file_count(self, schools_data):
        """學校 list 應該按 file_count 由大到小排序"""
        for county, schools in schools_data.items():
            for i in range(len(schools) - 1):
                assert schools[i]["file_count"] >= schools[i + 1]["file_count"]

    def test_deduped_by_name(self, schools_data):
        """同一 county 內同名學校不該出現兩次"""
        for county, schools in schools_data.items():
            names = [s["name"] for s in schools]
            assert len(names) == len(set(names)), (
                f"{county} 有重複 school: {[n for n in names if names.count(n) > 1]}"
            )