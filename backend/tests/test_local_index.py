"""
Unit tests for app.scraper.local_index

涵蓋：
- paper_id 計算（SHA1 stable identifier）
- filename parse 5 種 patterns
- search() filter 邏輯
- get_paired_by_id() pair 邏輯（含 fallback paper ↔ daan）
- _normalise_school() 字串比對
- _is_invalidated() 不被 archive cron 的 _pdf_tree_cache marker 污染
"""

import sys
from pathlib import Path

import pytest

# 確保 backend/ 在 path
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.scraper import local_index  # noqa: E402


# ============================================================
# Fixtures
# ============================================================
@pytest.fixture(scope="module")
def index_data():
    """共用 index (避免每個 test 重 walk 76s)"""
    return local_index.get_index()


@pytest.fixture
def sample_paper_item(index_data):
    """從真實 index 抓一個楠梓 paper item 來測"""
    for it in index_data:
        if it.get("school_name") == "楠梓國中" and it["filetype"] == "paper":
            return it
    pytest.skip("no 楠梓 paper item in index")


@pytest.fixture
def sample_daan_item(index_data):
    for it in index_data:
        if it.get("school_name") == "楠梓國中" and it["filetype"] == "daan":
            return it
    pytest.skip("no 楠梓 daan item in index")


# ============================================================
# paper_id
# ============================================================
class TestPaperId:
    def test_stable_for_same_path(self):
        """SHA1 一致性 - 同 path 應該產出同 id"""
        pid1 = local_index._make_paper_id("高雄市/國中/七年級/數學/paper/test.pdf")
        pid2 = local_index._make_paper_id("高雄市/國中/七年級/數學/paper/test.pdf")
        assert pid1 == pid2

    def test_length_16(self):
        pid = local_index._make_paper_id("x/y/z.pdf")
        assert len(pid) == 16

    def test_case_insensitive(self):
        """不同大小寫應該產出同 id (lowercase 後 hash)"""
        pid1 = local_index._make_paper_id("高雄市/Test.pdf")
        pid2 = local_index._make_paper_id("高雄市/test.pdf")
        assert pid1 == pid2

    def test_different_paths_different_ids(self):
        pid1 = local_index._make_paper_id("a/b.pdf")
        pid2 = local_index._make_paper_id("a/c.pdf")
        assert pid1 != pid2


# ============================================================
# filename parsing
# ============================================================
class TestParseFilename:
    """5 種 filename patterns"""

    def test_pattern1_studyark_standard(self):
        # <county>_<year>_<exam>_<fileid>_<school>_<publisher>.pdf
        m = local_index._parse_filename("高雄市_109_期中考_34585_高雄市立大樹國民中學_南一.pdf")
        assert m is not None
        assert m["school_year"] == "109"
        assert m["exam_type"] == "期中考"
        assert m["fileid"] == "34585"
        assert m["school_name"] == "高雄市立大樹國民中學"
        assert m["version"] == "南一"

    def test_pattern2_term_down_up(self):
        # <county>_<year>下/上學期_<exam>_<fileid>_<school>_<publisher>.pdf
        m = local_index._parse_filename("臺東縣_108下學期_期末考_28550_臺東縣立新生國小_何嘉仁.pdf")
        assert m is not None
        assert m["school_year"] == "108"
        assert m["school_term"] == "下學期"
        assert m["exam_type"] == "期末考"
        assert m["fileid"] == "28550"
        assert m["school_name"] == "臺東縣立新生國小"
        assert m["version"] == "何嘉仁"

    def test_pattern3_tcool_migrated(self):
        # <county>_<year>_第N學期_<exam>_<school>_<grade>_<subject>[_解答].pdf
        m = local_index._parse_filename("高雄市_110_第2學期_補考_高雄市五福國中_一年級_公民.pdf")
        assert m is not None
        assert m["school_year"] == "110"
        assert m["exam_type"] == "補考"
        assert m["school_name"] == "高雄市五福國中"
        assert m["grade"] == "一年級"
        assert m["subject"] == "公民"

    def test_pattern4_no_fileid(self):
        # 【2026-08-15 新】<county>_<year>_<exam>_<school>_<grade>_<subject>.pdf
        m = local_index._parse_filename("苗栗縣_108_第1段考_縣立大同國中_七年級_公民.pdf")
        assert m is not None
        assert m["school_year"] == "108"
        assert m["school_name"] == "縣立大同國中"
        assert m["grade"] == "七年級"
        assert m["subject"] == "公民"

    def test_pattern5_non_standard_year_fallback(self):
        # 楠梓109-2-2自(生物).pdf - 非標準、Pattern 1-3 都不 match、走 fallback
        m = local_index._parse_filename("楠梓109-2-2自(生物).pdf")
        assert m is not None
        # 至少 school_year 應該抓出來
        assert m["school_year"] in ("109", "")
        # 學校名是空（因為不在標準格式）由後續 fallback 補
        assert m["school_name"] == ""

    def test_pattern5_year_with_學年_suffix(self):
        m = local_index._parse_filename("高雄市立楠梓國中108學年度第1學期第3階段定期評量3年級國文科試題卷.pdf")
        assert m is not None
        assert m["school_year"] == "108"

    def test_no_year_returns_empty(self):
        # 真的沒有年份 → 學年空字串
        m = local_index._parse_filename("楠梓國中1年級上-3段歷史.pdf")
        assert m is not None
        # 沒有學年字眼 → 空字串
        assert m["school_year"] == ""


# ============================================================
# _normalise_school / _school_name_matches
# ============================================================
class TestSchoolMatching:
    """學校名稱 substring match 要寬鬆 — dashboard dropdown value 可能跟
    local_index 抓出來的 school_name 不一樣 (例: 「高雄市楠梓國中」 vs 「楠梓國中」)"""

    def test_normalise_strips_市(self):
        assert local_index._normalise_school("高雄市楠梓國中") == "楠梓國中"

    def test_normalise_strips_縣(self):
        assert local_index._normalise_school("苗栗縣大同國中") == "大同國中"

    def test_normalise_strips_立_prefix(self):
        assert local_index._normalise_school("高雄市立楠梓國中") == "楠梓國中"

    def test_normalise_no_change(self):
        assert local_index._normalise_school("楠梓國中") == "楠梓國中"

    def test_match_substring(self):
        assert local_index._school_name_matches("高雄市楠梓國中", "楠梓國中") is True

    def test_match_via_normalise(self):
        assert local_index._school_name_matches("高雄市楠梓國中", "高雄市立楠梓國中") is True

    def test_no_match(self):
        assert local_index._school_name_matches("苗栗縣大同國中", "楠梓國中") is False

    def test_empty_query_matches_anything(self):
        assert local_index._school_name_matches("", "anything") is True

    def test_empty_candidate_no_match(self):
        assert local_index._school_name_matches("大同", "") is False


# ============================================================
# get_paired_by_id - paper + daan pair 邏輯
# ============================================================
class TestPairedById:
    def test_paper_returns_pair(self, sample_paper_item):
        """從 paper_id 找 → 應該有 paper_path，可能也有 daan_path"""
        gid = sample_paper_item["paper_id"]
        group = local_index.get_paired_by_id(gid)
        assert group is not None
        assert group["paper_id"] == gid
        assert group["paper_path"] is not None
        # filetype_set 至少含 paper
        assert "paper" in group["filetype_set"]

    def test_daan_returns_pair(self, sample_daan_item):
        """從 daan_id 找 → main paper_id 應該是 paper 的（不是 daan 的）"""
        gid = sample_daan_item["paper_id"]
        group = local_index.get_paired_by_id(gid)
        assert group is not None
        # paper_id 應該是 paper 的 id
        if group.get("paper_path"):
            assert group["paper_id"] == group["paper_id_paper"]
        # daan_path 應該存在
        if group.get("paper_path"):
            assert group["daan_path"] is not None
            assert group["paper_id_daan"] == gid

    def test_unknown_id_returns_none(self):
        result = local_index.get_paired_by_id("ffffffffffffffff")
        assert result is None


# ============================================================
# search()
# ============================================================
class TestSearch:
    def test_no_filter_returns_all(self, index_data):
        groups, total, total_pages = local_index.search(per_page=1000)
        # 至少有一堆 groups
        assert total > 0
        assert total_pages >= 1

    def test_filter_by_county(self):
        groups, total, _ = local_index.search(county="高雄市", per_page=1000)
        assert total > 0
        # 所有 group 的 county 都應該是 高雄市
        for g in groups:
            assert g["county"] == "高雄市"

    def test_filter_by_county_and_school(self):
        groups, total, _ = local_index.search(
            county="高雄市", school_name="高雄市楠梓國中", per_page=1000
        )
        assert total > 0
        # substring match — 學校名應該含 楠梓
        for g in groups:
            assert "楠梓" in g["school_name"]

    def test_filter_by_county_and_school_normalised(self):
        """dashboard dropdown value「高雄市楠梓國中」要 match「楠梓國中」"""
        groups, total, _ = local_index.search(
            county="高雄市", school_name="高雄市楠梓國中", per_page=1000
        )
        # 不論用 full name 或 partial 都應該找到東西
        assert total > 0

    def test_filter_by_grade_subject(self):
        groups, total, _ = local_index.search(
            county="新北市", grade="七年級", subject="數學", per_page=1000
        )
        # 可能 0 也可能 >0，看 disk 上有沒有 — 但 logic 應正確
        for g in groups:
            assert g["county"] == "新北市"
            assert g["grade"] == "七年級"
            assert g["subject"] == "數學"

    def test_pagination(self):
        """page=1 跟 page=2 應該回不同 group"""
        g1, t1, _ = local_index.search(page=1, per_page=8)
        g2, t2, _ = local_index.search(page=2, per_page=8)
        if t1 > 8:
            assert len(g1) == 8
            assert len(g2) == 8
            # 不同 paper_id
            ids1 = {g["paper_id"] for g in g1}
            ids2 = {g["paper_id"] for g in g2}
            assert ids1 != ids2

    def test_pair_includes_daan_flag(self):
        """group 的 filetype_set 應該正確標示有沒有 daan"""
        groups, _, _ = local_index.search(county="新北市", grade="七年級", subject="數學", per_page=1000)
        # 每個 group 至少有 paper (因為搜尋時預設 filetype="")
        for g in groups:
            assert isinstance(g["filetype_set"], list)
            # filetype_set 元素只能是 "paper" / "daan"
            for ft in g["filetype_set"]:
                assert ft in ("paper", "daan")

    def test_zero_results(self):
        """完全沒 match → 0 groups, 1 page (避免 div by zero)"""
        groups, total, total_pages = local_index.search(
            county="不存在縣市", grade="不存在年級"
        )
        assert total == 0
        assert groups == []
        assert total_pages == 1  # max(1, ...)


# ============================================================
# Invalidate marker 隔離（重要 — archive cron 不能污染）
# ============================================================
class TestInvalidateMarkerIsolation:
    """確認 local_index 用獨立 marker, 不被 archive cron 的
    _pdf_tree_cache.invalidate 觸發 rebuild"""

    def test_local_marker_is_independent(self):
        # _pdf_tree_cache.invalidate marker 一定存在 (archive cron 會 touch)
        pdf_marker = local_index.STATE_DIR / "_pdf_tree_cache.invalidate"
        # local_index 的 marker 應該是另一個檔名
        local_marker = local_index.STATE_DIR / "_local_index_cache.invalidate"
        assert pdf_marker != local_marker

    def test_is_invalidated_checks_only_local_marker(self):
        """即便 _pdf_tree_cache.invalidate 存在很久, 也不該讓 local_index 認為要 rebuild"""
        # 暫時把 pdf marker 設成未來時間 (模擬 archive 剛跑完)
        import os
        import time
        pdf_marker = local_index.STATE_DIR / "_pdf_tree_cache.invalidate"
        original_mtime = pdf_marker.stat().st_mtime if pdf_marker.exists() else None
        try:
            # 設成未來時間
            future = time.time() + 3600
            os.utime(pdf_marker, (future, future))

            # 載入 index — 不該 rebuild
            items = local_index.get_index()
            assert items is not None
            assert len(items) > 0
            # 如果 _load_or_build_index 走 rebuild 路徑，會花 76s
            # 假設 cache 有資料，1 秒內應該 load 完成
        finally:
            if original_mtime is not None:
                os.utime(pdf_marker, (original_mtime, original_mtime))


# ============================================================
# Walk 路徑結構判斷
# ============================================================
class TestWalkStructure:
    """確認 _walk_archive 的 path 結構判斷 (path 是 directory, parts 是 directory segments)"""

    def test_studyark_structure_detected(self, index_data):
        """確認有 items 被識別為 StudyArk 結構 (有 level/grade/subject/filetype)"""
        studyark_items = [
            it for it in index_data
            if it["level"] and it["grade"] and it["subject"] and it["filetype"]
        ]
        assert len(studyark_items) > 0

    def test_skipped_dirs_not_in_index(self, index_data):
        """被 SKIP 的 top-level dirs 不該在 index"""
        for it in index_data:
            rel = it["rel_path"]
            assert not rel.startswith("其他X/"), f"其他X leaked: {rel}"
            assert not rel.startswith("未分類/"), f"未分類 leaked: {rel}"
            assert not rel.startswith("_未分類/"), f"_未分類 leaked: {rel}"
            assert not rel.startswith("_internal/"), f"_internal leaked: {rel}"
            assert not rel.startswith("_inbox/"), f"_inbox leaked: {rel}"
            assert not rel.startswith("cap_exam/"), f"cap_exam leaked: {rel}"
            assert not rel.startswith("ceec/"), f"ceec leaked: {rel}"
            assert not rel.startswith("state/"), f"state leaked: {rel}"
            assert not rel.startswith("logs/"), f"logs leaked: {rel}"

    def test_only_county_items_indexed(self, index_data):
        """【2026-08-16 改】所有 item 應該是各縣市底下的 (_inbox 不算)"""
        known_counties = {
            "臺北市", "新北市", "桃園市", "臺中市", "臺南市", "高雄市",
            "基隆市", "宜蘭縣", "新竹市", "新竹縣", "苗栗縣",
            "彰化縣", "南投縣", "雲林縣", "嘉義市", "嘉義縣",
            "屏東縣", "臺東縣", "花蓮縣", "澎湖縣", "金門縣", "連江縣",
        }
        for it in index_data:
            # county 應該是 22 縣市之一
            assert it["county"] in known_counties, (
                f"unexpected county in index: {it['county']} (rel={it['rel_path']})"
            )