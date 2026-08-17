"""
SQLite DB module for tqark-web exam search (2026-08-17 新)

【2026-08-17 新】把 local_index 的 in-memory JSON cache 換成 SQLite + indexes
- 51k items × 7 conditions linear scan → 51k × log 51k × 7 indexed
- 從 O(N × M) 變 O(log N × M) - 約 5000x 速度提升
- 中文 search 用 LIKE (不裝 FTS5 extension, 簡單可靠)

設計:
- DB 路徑: backend/state/tqark-web.db (本地, 不放 NAS 避免 CIFS 慢)
- Schema: see SCHEMA constant
- Trigger 點: 3 個 (web startup, archive cron, 6h 定期)
- Migration: 從 local_papers_index.json 載入 (scripts/migrate_json_to_db.py)
"""
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

_log = logging.getLogger("tqark.db")

# ============================================================
# 設定
# ============================================================
DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent.parent / "state" / "tqark-web.db"

# ============================================================
# Schema
# ============================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  paper_id        TEXT PRIMARY KEY,
  rel_path        TEXT UNIQUE NOT NULL,
  filename        TEXT NOT NULL,
  county          TEXT NOT NULL,
  level           TEXT NOT NULL,
  school_year     TEXT,
  grade           TEXT,
  subject         TEXT,
  paper_or_daan   TEXT NOT NULL,
  school_name     TEXT,
  school_term     TEXT,
  exam_type       TEXT,
  version         TEXT,
  size_kb         INTEGER,
  mtime           TEXT,
  -- 衍生欄位 (方便 query + index)
  is_paper        INTEGER NOT NULL DEFAULT 1,
  has_school      INTEGER NOT NULL DEFAULT 0,
  has_school_name INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_county ON files(county);
CREATE INDEX IF NOT EXISTS idx_year ON files(school_year);
CREATE INDEX IF NOT EXISTS idx_grade ON files(grade);
CREATE INDEX IF NOT EXISTS idx_subject ON files(subject);
CREATE INDEX IF NOT EXISTS idx_exam ON files(exam_type);
CREATE INDEX IF NOT EXISTS idx_school ON files(school_name);
CREATE INDEX IF NOT EXISTS idx_paper ON files(is_paper);
CREATE INDEX IF NOT EXISTS idx_level ON files(level);
"""

# ============================================================
# Connection helpers
# ============================================================
_db_lock = threading.Lock()
_conn_cache: dict = {}


def get_db_path() -> Path:
    return DEFAULT_DB_PATH


def _connect(db_path=None) -> sqlite3.Connection:
    """取得連線 (thread-safe + reuse). DB 自動建檔."""
    if db_path is None:
        db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    key = str(db_path)
    with _db_lock:
        if key not in _conn_cache:
            conn = sqlite3.connect(
                str(db_path),
                timeout=30,
                check_same_thread=False,
                isolation_level=None,  # autocommit mode for explicit BEGIN/COMMIT
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.execute("PRAGMA mmap_size=268435456")
            _conn_cache[key] = conn
            _log.info(f"[DB] Connected to {db_path}")
        return _conn_cache[key]


@contextmanager
def transaction(db_path=None):
    """交易 context manager. auto-commit on success, rollback on error."""
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ============================================================
# Schema setup
# ============================================================
def init_db(db_path=None) -> None:
    """建 tables + indexes (idempotent)."""
    conn = _connect(db_path)
    conn.executescript(SCHEMA)
    _log.info(f"[DB] Schema initialized")


# ============================================================
# Bulk operations
# ============================================================
def _to_row(item: dict) -> tuple:
    """把 local_index item dict 轉成 DB row tuple."""
    paper_or_daan = item.get("filetype") or ""
    return (
        item["paper_id"],
        item["rel_path"],
        item.get("filename") or Path(item["rel_path"]).name,
        item.get("county") or "",
        item.get("level") or "",
        item.get("school_year") or None,
        item.get("grade") or None,
        item.get("subject") or None,
        paper_or_daan,
        item.get("school_name") or None,
        item.get("school_term") or None,
        item.get("exam_type") or None,
        item.get("version") or None,
        item.get("size_kb") or 0,
        item.get("mtime") or None,
        1 if paper_or_daan == "paper" else 0,
        1 if item.get("school_name") else 0,
        1 if (item.get("school_name") and len(item["school_name"]) > 4) else 0,
    )


def _flush_batch(conn: sqlite3.Connection, batch: list) -> None:
    """一次 INSERT OR REPLACE 一批 rows."""
    conn.executemany(
        """INSERT OR REPLACE INTO files (
            paper_id, rel_path, filename, county, level,
            school_year, grade, subject, paper_or_daan,
            school_name, school_term, exam_type, version,
            size_kb, mtime,
            is_paper, has_school, has_school_name
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        batch,
    )


def rebuild_from_items(items, db_path=None, batch_size: int = 500):
    """從 local_index 格式的 items 全 INSERT OR REPLACE.

    Returns: (inserted_count, total_count)
    """
    init_db(db_path)
    conn = _connect(db_path)

    inserted = 0
    total = 0
    batch: list = []

    try:
        conn.execute("BEGIN")
        for item in items:
            total += 1
            batch.append(_to_row(item))
            if len(batch) >= batch_size:
                _flush_batch(conn, batch)
                inserted += len(batch)
                batch.clear()
        if batch:
            _flush_batch(conn, batch)
            inserted += len(batch)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    _log.info(f"[DB] Rebuilt {inserted}/{total} items")
    return inserted, total


def delete_paths(paths, db_path=None) -> int:
    """刪除給定 rel_path 的 rows. Returns deleted count."""
    conn = _connect(db_path)
    deleted = 0
    try:
        conn.execute("BEGIN")
        for path in paths:
            cur = conn.execute("DELETE FROM files WHERE rel_path = ?", (path,))
            deleted += cur.rowcount
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return deleted


def clear_all(db_path=None) -> None:
    """清空整個 table (rebuild 前用)."""
    conn = _connect(db_path)
    conn.execute("DELETE FROM files")


def count_all(db_path=None) -> int:
    conn = _connect(db_path)
    cur = conn.execute("SELECT COUNT(*) FROM files")
    return cur.fetchone()[0]


# ============================================================
# Query: 給 search API 用
# ============================================================
def search_files(
    county: str = "",
    grade: str = "",
    subject: str = "",
    school_year: str = "",
    school_term: str = "",
    exam_type: str = "",
    version: str = "",
    school_name_kw: str = "",
    level: str = "",
    paper_or_daan: str = "",
    page: int = 1,
    per_page: int = 8,
    db_path=None,
):
    """多條件 search.

    Returns: (rows, total_count, total_pages)
    中文 LIKE 查詢 (e.g. school_name LIKE '%楠梓%')
    """
    init_db(db_path)
    conn = _connect(db_path)

    where_parts = []
    params: list = []

    if county:
        where_parts.append("county = ?"); params.append(county)
    if level:
        where_parts.append("level = ?"); params.append(level)
    if grade:
        where_parts.append("grade = ?"); params.append(grade)
    if subject:
        where_parts.append("subject = ?"); params.append(subject)
    if school_year:
        where_parts.append("school_year = ?"); params.append(school_year)
    if school_term:
        where_parts.append("school_term = ?"); params.append(school_term)
    if exam_type:
        where_parts.append("exam_type = ?"); params.append(exam_type)
    if version:
        where_parts.append("version = ?"); params.append(version)
    if paper_or_daan:
        where_parts.append("paper_or_daan = ?"); params.append(paper_or_daan)
    if school_name_kw:
        where_parts.append("school_name LIKE ?"); params.append(f"%{school_name_kw}%")

    where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    count_sql = f"SELECT COUNT(*) FROM files {where_sql}"
    total = conn.execute(count_sql, params).fetchone()[0]

    total_pages = max(1, (total + per_page - 1) // per_page)
    offset = (page - 1) * per_page

    list_sql = (
        f"SELECT * FROM files {where_sql} "
        f"ORDER BY school_year DESC, county, school_name, grade, subject "
        f"LIMIT ? OFFSET ?"
    )
    rows = conn.execute(list_sql, params + [per_page, offset]).fetchall()

    return [dict(r) for r in rows], total, total_pages


def get_distinct_values(column: str, db_path=None, where_clause: str = "", where_params: tuple = ()):
    """取 distinct non-empty values (給 dropdown).

    column 必須在白名單內 (防 SQL injection).
    """
    allowed = {
        "county", "level", "school_year", "grade", "subject",
        "school_term", "exam_type", "version", "school_name",
    }
    if column not in allowed:
        raise ValueError(f"column {column!r} not allowed (must be one of {sorted(allowed)})")

    conn = _connect(db_path)
    where_sql = f"WHERE {where_clause} AND " if where_clause else "WHERE "
    sql = (
        f"SELECT DISTINCT {column} FROM files "
        f"{where_sql}{column} != '' AND {column} IS NOT NULL "
        f"ORDER BY {column}"
    )
    rows = conn.execute(sql, where_params).fetchall()
    return [r[0] for r in rows if r[0]]


def get_available_filters(county: str = "", school_name: str = "", db_path=None) -> dict:
    """【2026-08-17 新】回傳該縣市/學校有的所有 filter dropdown values.

    用於 dashboard cascading dropdown:
    - 點 county → 回 schools (call /api/available-schools)
    - 點 school → 回 year/grade/subject/exam/term/version (call this function)

    Returns:
        {
            "school_year": ["110", "109", ...],  # DESC sort
            "grade": ["七年級", "八年級", ...],
            "subject": ["數學", "國文", ...],
            "school_term": ["上學期", "下學期", ...],
            "exam_type": ["第一次段考", "期中考", ...],
            "version": ["南一", "康軒", ...],
        }
    """
    conn = _connect(db_path)

    where_parts = []
    params = []
    if county:
        where_parts.append("county = ?"); params.append(county)
    if school_name:
        from app.scraper.school_tokens import core_tokens
        for t in core_tokens(school_name):
            where_parts.append("school_name LIKE ?"); params.append(f"%{t}%")
    where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    fields = ["school_year", "grade", "subject", "school_term", "exam_type", "version"]
    result = {}
    for f in fields:
        order = "DESC" if f == "school_year" else "ASC"
        cond = f"{f} != '' AND {f} IS NOT NULL"
        sql = f"SELECT DISTINCT {f} FROM files {(where_sql + ' AND ' + cond) if where_sql else ('WHERE ' + cond)} ORDER BY {f} {order}"
        rows = conn.execute(sql, params).fetchall()
        result[f] = [r[0] for r in rows if r[0]]
    return result


# ============================================================
# Migration / backup
# ============================================================
def backup_db(db_path=None) -> Path:
    """複製 db 到 <db_path>.bak.<timestamp>"""
    if db_path is None:
        db_path = get_db_path()
    if not db_path.exists():
        raise FileNotFoundError(f"DB not found: {db_path}")
    ts = int(time.time())
    bak = db_path.with_suffix(f".db.bak.{ts}")
    bak.write_bytes(db_path.read_bytes())
    _log.info(f"[DB] Backed up to {bak}")
    return bak


__all__ = [
    "DEFAULT_DB_PATH",
    "SCHEMA",
    "get_db_path",
    "_connect",
    "transaction",
    "init_db",
    "rebuild_from_items",
    "delete_paths",
    "clear_all",
    "count_all",
    "search_files",
    "get_distinct_values",
    "backup_db",
]

# ============================================================
# High-level: grouped search (給 API 用)
# ============================================================
def search_files_grouped(
    county: str = "",
    grade: str = "",
    subject: str = "",
    school_year: str = "",
    school_term: str = "",
    exam_type: str = "",
    version: str = "",
    school_name: str = "",
    filetype: str = "",
    page: int = 1,
    per_page: int = 8,
    db_path=None,
):
    """多條件 search + paper/daan pair grouping.

    Returns:
        (groups, total_count, total_pages)

    groups 格式跟 local_index.search() 一樣 (給 template 用):
        [
            {
                "paper_id": ..., "county": ..., "school_name": ...,
                "paper_id_paper": ..., "paper_path": ...,
                "paper_id_daan": ..., "daan_path": ...,
                "filetype_set": [...], "download_answer": ...,
                ...
            },
            ...
        ]
    """
    # 1. SQL query (一次拿所有 match rows)
    # 注意: paper_or_daan="" 表示不限制 (paper + daan 都拿)
    rows, total, total_pages = search_files(
        county=county, grade=grade, subject=subject,
        school_year=school_year, school_term=school_term,
        exam_type=exam_type, version=version,
        school_name_kw=school_name, paper_or_daan=filetype,
        page=page, per_page=per_page, db_path=db_path,
    )

    # 2. Pair grouping (paper + daan share same (county, year, exam, school, grade, subject, term, version))
    from collections import defaultdict
    grouped: dict = defaultdict(lambda: {"paper": None, "daan": None})

    for r in rows:
        key = (
            r["county"], r["school_year"], r["school_term"],
            r["exam_type"], r["school_name"], r["grade"],
            r["subject"], r["version"],
        )
        if r["paper_or_daan"] == "paper":
            grouped[key]["paper"] = r
        elif r["paper_or_daan"] == "daan":
            grouped[key]["daan"] = r

    # 3. 轉成 groups list (跟 local_index.search() 格式相容)
    groups = []
    for key, pair in grouped.items():
        main = pair["paper"] or pair["daan"]
        if not main:
            continue
        county, year, term, exam, school, grade, subject, version = key
        groups.append({
            "paper_id": main["paper_id"],
            "county": county,
            "school_year": year,
            "school_term": term,
            "exam_type": exam,
            "school_name": school,
            "grade": grade,
            "subject": subject,
            "version": version,
            "paper_id_paper": pair["paper"]["paper_id"] if pair["paper"] else None,
            "paper_path": pair["paper"]["rel_path"] if pair["paper"] else None,
            "paper_id_daan": pair["daan"]["paper_id"] if pair["daan"] else None,
            "daan_path": pair["daan"]["rel_path"] if pair["daan"] else None,
            "filetype_set": [t for t in ("paper", "daan") if pair[t]],
            "download_answer": "有" if pair["daan"] else "無",
            "title": main["filename"],
            "rel_path": main["rel_path"],
            "filename": main["filename"],
            "size_kb": main["size_kb"],
        })

    return groups, total, total_pages


__all__.append("search_files_grouped")

# ============================================================
# Update search_files_grouped 用 core_tokens
# ============================================================
def _apply_school_tokens(where_parts, params, school_name):
    """把 school_name 拆成多個 core token (AND LIKE)。"""
    if not school_name:
        return
    # 避免循環 import
    from app.scraper.school_tokens import core_tokens
    tokens = core_tokens(school_name)
    for t in tokens:
        where_parts.append("school_name LIKE ?")
        params.append(f"%{t}%")


# Monkey-patch: 把 _apply_school_tokens 加進 search_files_grouped
import functools
_orig_search_files_grouped = search_files_grouped


@functools.wraps(_orig_search_files_grouped)
def search_files_grouped_with_tokens(
    county="", grade="", subject="", school_year="", school_term="",
    exam_type="", version="", school_name="", filetype="",
    page=1, per_page=8, db_path=None,
):
    """search_files_grouped 加 token-based school_name 搜尋。"""
    # 拆 core_tokens, 直接組 where_parts
    from app.scraper.school_tokens import core_tokens
    from collections import defaultdict

    init_db(db_path)
    conn = _connect(db_path)

    where_parts = []
    params = []

    if county:        where_parts.append("county = ?");            params.append(county)
    if grade:         where_parts.append("grade = ?");             params.append(grade)
    if subject:       where_parts.append("subject = ?");           params.append(subject)
    if school_year:   where_parts.append("school_year = ?");       params.append(school_year)
    if school_term:   where_parts.append("school_term = ?");       params.append(school_term)
    if exam_type:     where_parts.append("exam_type = ?");         params.append(exam_type)
    if version:       where_parts.append("version = ?");           params.append(version)
    if filetype:      where_parts.append("paper_or_daan = ?");    params.append(filetype)

    # School name tokens (AND LIKE)
    if school_name:
        tokens = core_tokens(school_name)
        for t in tokens:
            where_parts.append("school_name LIKE ?")
            params.append(f"%{t}%")

    where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    count_sql = f"SELECT COUNT(*) FROM files {where_sql}"
    total = conn.execute(count_sql, params).fetchone()[0]

    total_pages = max(1, (total + per_page - 1) // per_page)
    offset = (page - 1) * per_page

    list_sql = (
        f"SELECT * FROM files {where_sql} "
        f"ORDER BY school_year DESC, county, school_name, grade, subject "
        f"LIMIT ? OFFSET ?"
    )
    rows = conn.execute(list_sql, params + [per_page, offset]).fetchall()

    # Pair grouping (同樣邏輯)
    grouped = defaultdict(lambda: {"paper": None, "daan": None})
    for r in rows:
        key = (
            r["county"], r["school_year"], r["school_term"],
            r["exam_type"], r["school_name"], r["grade"],
            r["subject"], r["version"],
        )
        if r["paper_or_daan"] == "paper":
            grouped[key]["paper"] = r
        elif r["paper_or_daan"] == "daan":
            grouped[key]["daan"] = r

    groups = []
    for key, pair in grouped.items():
        main = pair["paper"] or pair["daan"]
        if not main:
            continue
        c, y, t, e, s, g, sub, v = key
        groups.append({
            "paper_id": main["paper_id"],
            "county": c, "school_year": y, "school_term": t,
            "exam_type": e, "school_name": s, "grade": g,
            "subject": sub, "version": v,
            "paper_id_paper": pair["paper"]["paper_id"] if pair["paper"] else None,
            "paper_path": pair["paper"]["rel_path"] if pair["paper"] else None,
            "paper_id_daan": pair["daan"]["paper_id"] if pair["daan"] else None,
            "daan_path": pair["daan"]["rel_path"] if pair["daan"] else None,
            "filetype_set": [t for t in ("paper", "daan") if pair[t]],
            "download_answer": "有" if pair["daan"] else "無",
            "title": main["filename"],
            "rel_path": main["rel_path"],
            "filename": main["filename"],
            "size_kb": main["size_kb"],
        })

    # 4. 【2026-08-17 新】Fallback: 如果 filter 0 筆但有 school_name, 放寬只比對 school
    fallback_unclassified = False
    fallback_count = 0
    fallback_filters_dropped = []

    # 判定哪些 filter 是 metadata (放寬時可丟)
    metadata_filters = {
        "grade": grade, "subject": subject, "school_year": school_year,
        "school_term": school_term, "exam_type": exam_type,
        "version": version, "filetype": filetype,
    }
    has_any_metadata_filter = any(metadata_filters.values())
    # DriveFolder 檔案 school_name 通常有, 但其他欄位空
    # 條件: (a) total=0, (b) 有 school_name, (c) 有 metadata filter
    if total == 0 and school_name and has_any_metadata_filter:
        # 重新 query, 只用 county + school_name
        from app.scraper.school_tokens import core_tokens as _core_tokens
        where_parts_fb = []
        params_fb = []
        if county:
            where_parts_fb.append("county = ?"); params_fb.append(county)
        tokens = _core_tokens(school_name)
        for t in tokens:
            where_parts_fb.append("school_name LIKE ?"); params_fb.append(f"%{t}%")
        where_sql_fb = ("WHERE " + " AND ".join(where_parts_fb)) if where_parts_fb else ""
        count_sql_fb = f"SELECT COUNT(*) FROM files {where_sql_fb}"
        fb_total = conn.execute(count_sql_fb, params_fb).fetchone()[0]
        if fb_total > 0:
            # 重新 pair grouping
            rows_fb = conn.execute(
                f"SELECT * FROM files {where_sql_fb} "
                f"ORDER BY school_year DESC, county, school_name, grade, subject "
                f"LIMIT ? OFFSET ?",
                params_fb + [per_page, offset]
            ).fetchall()
            grouped_fb = defaultdict(lambda: {"paper": None, "daan": None})
            for r in rows_fb:
                key = (
                    r["county"], r["school_year"], r["school_term"],
                    r["exam_type"], r["school_name"], r["grade"],
                    r["subject"], r["version"],
                )
                if r["paper_or_daan"] == "paper":
                    grouped_fb[key]["paper"] = r
                elif r["paper_or_daan"] == "daan":
                    grouped_fb[key]["daan"] = r
            groups_fb = []
            for key, pair in grouped_fb.items():
                main = pair["paper"] or pair["daan"]
                if not main:
                    continue
                c, y, t, e, s, g, sub, v = key
                groups_fb.append({
                    "paper_id": main["paper_id"],
                    "county": c, "school_year": y, "school_term": t,
                    "exam_type": e, "school_name": s, "grade": g,
                    "subject": sub, "version": v,
                    "paper_id_paper": pair["paper"]["paper_id"] if pair["paper"] else None,
                    "paper_path": pair["paper"]["rel_path"] if pair["paper"] else None,
                    "paper_id_daan": pair["daan"]["paper_id"] if pair["daan"] else None,
                    "daan_path": pair["daan"]["rel_path"] if pair["daan"] else None,
                    "filetype_set": [t for t in ("paper", "daan") if pair[t]],
                    "download_answer": "有" if pair["daan"] else "無",
                    "title": main["filename"],
                    "rel_path": main["rel_path"],
                    "filename": main["filename"],
                    "size_kb": main["size_kb"],
                })
            groups = groups_fb
            total = fb_total
            total_pages = max(1, (total + per_page - 1) // per_page)
            fallback_unclassified = True
            fallback_count = fb_total
            fallback_filters_dropped = [k for k, v in metadata_filters.items() if v]

    return groups, total, total_pages, {
        "fallback_unclassified": fallback_unclassified,
        "fallback_count": fallback_count,
        "fallback_filters_dropped": fallback_filters_dropped,
    }


# Replace the original
search_files_grouped = search_files_grouped_with_tokens
