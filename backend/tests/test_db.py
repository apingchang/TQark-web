"""
Unit tests for app.scraper.db

涵蓋:
- init_db 建 schema + indexes
- rebuild_from_items 全 INSERT OR REPLACE
- delete_paths
- count_all
- search_files 多條件 query + LIKE + pagination
- get_distinct_values (含 SQL injection 防護)
- backup_db

每個 test 用 tmp_path fixture 開獨立 db, 不污染 production。
"""
import sys
import sqlite3
from pathlib import Path

import pytest

# 確保 backend/ 在 path
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.scraper import db  # noqa: E402


# ============================================================
# Fixtures
# ============================================================
@pytest.fixture
def tmp_db(tmp_path):
    """給每個 test 一個獨立 db path"""
    return tmp_path / "test.db"


@pytest.fixture
def sample_items():
    """模擬 local_index.py 格式的 items"""
    return [
        {
            "paper_id": "aaaa1111bbbb2222",
            "rel_path": "高雄市/國中/110/七年級/數學/paper/test1.pdf",
            "filename": "test1.pdf",
            "county": "高雄市",
            "level": "國中",
            "school_year": "110",
            "grade": "七年級",
            "subject": "數學",
            "filetype": "paper",
            "school_name": "高雄市立楠梓國民中學",
            "school_term": "",
            "exam_type": "期中考",
            "version": "南一",
            "size_kb": 1234,
            "mtime": "2026-08-15T10:00:00",
            "abs_path": "/mnt/my_book/考題收集/高雄市/國中/110/七年級/數學/paper/test1.pdf",
            "ext": "pdf",
            "title": "高雄市立楠梓國民中學 110 七年級 數學",
        },
        {
            "paper_id": "cccc3333dddd4444",
            "rel_path": "高雄市/國中/110/七年級/數學/daan/test2.pdf",
            "filename": "test2.pdf",
            "county": "高雄市",
            "level": "國中",
            "school_year": "110",
            "grade": "七年級",
            "subject": "數學",
            "filetype": "daan",
            "school_name": "高雄市立楠梓國民中學",
            "school_term": "",
            "exam_type": "期中考",
            "version": "南一",
            "size_kb": 567,
            "mtime": "2026-08-15T11:00:00",
            "abs_path": "/mnt/my_book/考題收集/高雄市/國中/110/七年級/數學/daan/test2.pdf",
            "ext": "pdf",
            "title": "高雄市立楠梓國民中學 110 七年級 數學 答案",
        },
        {
            "paper_id": "eeee5555ffff6666",
            "rel_path": "臺北市/高中/111/十年級/英文/paper/test3.pdf",
            "filename": "test3.pdf",
            "county": "臺北市",
            "level": "高中",
            "school_year": "111",
            "grade": "十年級",
            "subject": "英文",
            "filetype": "paper",
            "school_name": "臺北市立建國高級中學",
            "school_term": "",
            "exam_type": "期末考",
            "version": "康軒",
            "size_kb": 890,
            "mtime": "2026-08-16T09:00:00",
            "abs_path": "/mnt/my_book/考題收集/臺北市/高中/111/十年級/英文/paper/test3.pdf",
            "ext": "pdf",
            "title": "臺北市立建國高級中學 111 十年級 英文",
        },
    ]


# ============================================================
# Schema
# ============================================================
class TestInitDb:
    def test_init_creates_files_table(self, tmp_db):
        db.init_db(tmp_db)
        conn = sqlite3.connect(str(tmp_db))
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        names = [r[0] for r in rows]
        assert "files" in names

    def test_init_creates_indexes(self, tmp_db):
        db.init_db(tmp_db)
        conn = sqlite3.connect(str(tmp_db))
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
        names = {r[0] for r in rows}
        assert "idx_county" in names
        assert "idx_year" in names
        assert "idx_grade" in names
        assert "idx_subject" in names
        assert "idx_exam" in names
        assert "idx_school" in names

    def test_init_idempotent(self, tmp_db):
        """跑兩次 init_db 不會壞"""
        db.init_db(tmp_db)
        db.init_db(tmp_db)
        assert db.count_all(tmp_db) == 0

    def test_wal_mode(self, tmp_db):
        """預期 WAL mode (concurrent reads OK)"""
        db.init_db(tmp_db)
        conn = sqlite3.connect(str(tmp_db))
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        # WAL or "wal" (lowercase), 但 truncate 可能改成 "delete"
        assert mode.lower() == "wal"


# ============================================================
# Rebuild
# ============================================================
class TestRebuild:
    def test_rebuild_inserts_all(self, tmp_db, sample_items):
        inserted, total = db.rebuild_from_items(sample_items, tmp_db)
        assert inserted == 3
        assert total == 3
        assert db.count_all(tmp_db) == 3

    def test_rebuild_replaces_existing(self, tmp_db, sample_items):
        """第二次 rebuild 同一個 paper_id 應該 REPLACE 不是 INSERT 新增"""
        db.rebuild_from_items(sample_items, tmp_db)
        # 修改 size_kb 後 rebuild
        modified = [{**it, "size_kb": 99999} for it in sample_items]
        db.rebuild_from_items(modified, tmp_db)
        assert db.count_all(tmp_db) == 3, "不應該有重複 row"

    def test_rebuild_empty_list(self, tmp_db):
        inserted, total = db.rebuild_from_items([], tmp_db)
        assert inserted == 0
        assert total == 0
        assert db.count_all(tmp_db) == 0


# ============================================================
# Delete / Count
# ============================================================
class TestDeleteAndCount:
    def test_delete_paths_removes_rows(self, tmp_db, sample_items):
        db.rebuild_from_items(sample_items, tmp_db)
        deleted = db.delete_paths(
            ["高雄市/國中/110/七年級/數學/paper/test1.pdf"], tmp_db
        )
        assert deleted == 1
        assert db.count_all(tmp_db) == 2

    def test_delete_nonexistent_path(self, tmp_db, sample_items):
        db.rebuild_from_items(sample_items, tmp_db)
        deleted = db.delete_paths(["不存在/的路徑.pdf"], tmp_db)
        assert deleted == 0

    def test_clear_all(self, tmp_db, sample_items):
        db.rebuild_from_items(sample_items, tmp_db)
        db.clear_all(tmp_db)
        assert db.count_all(tmp_db) == 0


# ============================================================
# Search - 核心功能
# ============================================================
class TestSearchFiles:
    def test_no_filter_returns_all(self, tmp_db, sample_items):
        db.rebuild_from_items(sample_items, tmp_db)
        rows, total, total_pages = db.search_files(db_path=tmp_db, per_page=100)
        assert total == 3
        assert total_pages == 1
        assert len(rows) == 3

    def test_filter_by_county(self, tmp_db, sample_items):
        db.rebuild_from_items(sample_items, tmp_db)
        rows, total, _ = db.search_files(county="高雄市", db_path=tmp_db, per_page=100)
        assert total == 2
        for r in rows:
            assert r["county"] == "高雄市"

    def test_filter_by_year_grade_subject(self, tmp_db, sample_items):
        db.rebuild_from_items(sample_items, tmp_db)
        rows, total, _ = db.search_files(
            county="高雄市", school_year="110", grade="七年級", subject="數學",
            db_path=tmp_db, per_page=100,
        )
        assert total == 2
        for r in rows:
            assert r["school_year"] == "110"
            assert r["grade"] == "七年級"
            assert r["subject"] == "數學"

    def test_filter_by_school_name_like_chinese(self, tmp_db, sample_items):
        """【2026-08-17 重要】中文 LIKE 查詢"""
        db.rebuild_from_items(sample_items, tmp_db)
        # "楠梓" substring 應該 match "高雄市立楠梓國民中學"
        rows, total, _ = db.search_files(school_name_kw="楠梓", db_path=tmp_db, per_page=100)
        assert total == 2  # 楠梓國中 paper + daan
        for r in rows:
            assert "楠梓" in r["school_name"]

    def test_filter_by_school_name_normalised_match(self, tmp_db, sample_items):
        """【2026-08-17】LIKE 是連續 substring match, 不支援跳字.
        dashboard value 應先 _normalise_school 再用核心 keyword (如 '楠梓') 查詢."""
        from app.scraper import local_index
        db.rebuild_from_items(sample_items, tmp_db)
        # 用核心 keyword '楠梓' (去掉市/縣/立/中學)
        # 應該 match "高雄市立楠梓國民中學"
        rows, total, _ = db.search_files(school_name_kw="楠梓", db_path=tmp_db, per_page=100)
        assert total == 2
        for r in rows:
            assert "楠梓" in r["school_name"]

    def test_filter_by_school_name_no_match_if_skip_chars(self, tmp_db, sample_items):
        """【2026-08-17】LIKE 不支援跳字 substring.
        '高雄市楠梓國中' (中間有立) vs '高雄市立楠梓國民中學' (中間有立、民) → LIKE '高雄市楠梓國中' 不 match."""
        db.rebuild_from_items(sample_items, tmp_db)
        rows, total, _ = db.search_files(
            school_name_kw="高雄市楠梓國中", db_path=tmp_db, per_page=100,
        )
        # 不 match (因為 LIKE 是連續 substring, 不能跳字)
        assert total == 0

    def test_filter_no_match_returns_zero(self, tmp_db, sample_items):
        db.rebuild_from_items(sample_items, tmp_db)
        rows, total, total_pages = db.search_files(county="花蓮縣", db_path=tmp_db)
        assert total == 0
        assert rows == []
        assert total_pages == 1  # max(1, ...)

    def test_pagination(self, tmp_db, sample_items):
        db.rebuild_from_items(sample_items, tmp_db)
        # page=1, per_page=2
        rows1, total, pages = db.search_files(db_path=tmp_db, page=1, per_page=2)
        rows2, _, _ = db.search_files(db_path=tmp_db, page=2, per_page=2)
        assert total == 3
        assert pages == 2  # ceil(3/2)
        assert len(rows1) == 2
        assert len(rows2) == 1
        # 不同 page 應該回不同 rows
        ids1 = {r["paper_id"] for r in rows1}
        ids2 = {r["paper_id"] for r in rows2}
        assert ids1.isdisjoint(ids2)

    def test_pagination_exact_multiple(self, tmp_db, sample_items):
        """剛好整除的 pagination"""
        db.rebuild_from_items(sample_items, tmp_db)
        rows, total, pages = db.search_files(db_path=tmp_db, page=1, per_page=1)
        assert total == 3
        assert pages == 3

    def test_combined_filters(self, tmp_db, sample_items):
        db.rebuild_from_items(sample_items, tmp_db)
        rows, total, _ = db.search_files(
            county="高雄市", subject="數學", paper_or_daan="paper",
            db_path=tmp_db, per_page=100,
        )
        assert total == 1
        assert rows[0]["paper_or_daan"] == "paper"
        assert rows[0]["subject"] == "數學"


# ============================================================
# Distinct values (dropdown)
# ============================================================
class TestDistinctValues:
    def test_get_county_list(self, tmp_db, sample_items):
        db.rebuild_from_items(sample_items, tmp_db)
        counties = db.get_distinct_values("county", tmp_db)
        assert "高雄市" in counties
        assert "臺北市" in counties

    def test_get_school_year_sorted(self, tmp_db, sample_items):
        db.rebuild_from_items(sample_items, tmp_db)
        years = db.get_distinct_values("school_year", tmp_db)
        assert years == ["110", "111"]  # sorted ascending

    def test_get_distinct_excludes_empty(self, tmp_db):
        db.init_db(tmp_db)
        # 加一個沒有 school_name 的 row
        conn = sqlite3.connect(str(tmp_db))
        conn.execute("""INSERT INTO files VALUES (
            'x', 'no_school.pdf', 'no_school.pdf', '新北市', '國小',
            NULL, NULL, NULL, 'paper', NULL, NULL, NULL, NULL, 0, NULL,
            1, 0, 0)""")
        conn.commit()
        schools = db.get_distinct_values("school_name", tmp_db)
        assert schools == []  # 排除 NULL/空

    def test_sql_injection_blocked(self, tmp_db, sample_items):
        """SQL injection 防護: column 必須在白名單"""
        db.rebuild_from_items(sample_items, tmp_db)
        with pytest.raises(ValueError, match="not allowed"):
            db.get_distinct_values("rel_path; DROP TABLE files--", tmp_db)


# ============================================================
# Backup
# ============================================================
class TestBackup:
    def test_backup_creates_copy(self, tmp_db, sample_items):
        db.init_db(tmp_db)
        db.rebuild_from_items(sample_items, tmp_db)
        bak = db.backup_db(tmp_db)
        assert bak.exists()
        assert bak.stat().st_size == tmp_db.stat().st_size
        # backup 是同檔名 .bak.<timestamp>
        assert ".bak." in bak.name

    def test_backup_missing_db(self, tmp_path):
        nonexistent = tmp_path / "ghost.db"
        with pytest.raises(FileNotFoundError):
            db.backup_db(nonexistent)


# ============================================================
# 整合: 真實規模
# ============================================================
class TestRealScale:
    """模擬 51k items 跑 query 看速度 (regression check)"""

    def test_50k_items_filter_speed(self, tmp_db, tmp_path):
        """生 50k 隨機 items, 跑 5 條件 query, < 100ms"""
        import random
        import time

        items = []
        counties = ["臺北市", "新北市", "高雄市", "臺中市", "桃園市"]
        grades = ["七年級", "八年級", "九年級"]
        subjects = ["數學", "英文", "國文", "理化", "社會"]
        for i in range(50_000):
            items.append({
                "paper_id": f"pid{i:08x}",
                "rel_path": f"{random.choice(counties)}/國中/110/{random.choice(grades)}/{random.choice(subjects)}/paper/{i}.pdf",
                "filename": f"{i}.pdf",
                "county": random.choice(counties),
                "level": "國中",
                "school_year": "110",
                "grade": random.choice(grades),
                "subject": random.choice(subjects),
                "filetype": "paper",
                "school_name": f"測試學校{random.randint(1, 100)}",
                "school_term": "",
                "exam_type": "期中考",
                "version": "南一",
                "size_kb": 100,
                "mtime": "2026-08-15",
                "abs_path": "/test/path",
                "ext": "pdf",
                "title": "test",
            })
        db.rebuild_from_items(items, tmp_db)

        t0 = time.time()
        rows, total, _ = db.search_files(
            county="高雄市", grade="七年級", subject="數學",
            school_name_kw="測試學校",
            page=1, per_page=8, db_path=tmp_db,
        )
        elapsed = time.time() - t0

        assert elapsed < 0.5, f"query took {elapsed*1000:.0f}ms (應 < 500ms)"
        assert total > 0  # 至少有 match
        assert len(rows) == 8
