"""
TQark-web archive status helpers (2026-08-24).

History
-------
* 2026-08-24: original implementation walked the entire ``/mnt/my_book/考題收集``
  tree four times (1× ``os.walk`` + 3× ``find`` subprocesses). On the CIFS
  mount each tree walk took minutes, so the status report stalled 10-20 minutes.
* 2026-08-24 (later): the same day we switched to the existing pre-built
  ``state/local_papers_index.json`` (built every 3h by ``db_rescan.py``) for
  the bulk 國小/國中/高中 classification. ``cap_exam`` and ``ceec`` are
  excluded from that index, so we still do **two** tiny direct walks for
  them — but never over the whole tree.

This module exposes :func:`collect_pdf_stats` (used by tests and
``tqark-archive-status``) plus :func:`render_summary` for the shell wrapper.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TypedDict


SKIP_TOP_DIRS: tuple[str, ...] = (
    "state",
    "logs",
    "_inbox",
    "_internal",
    "_未分類",
    "_待分類",
    "其他X",
    "未分類",
)


LEVEL_PAPER_KEYS: tuple[str, ...] = ("國小", "國中", "高中")


class PdfStats(TypedDict):
    pdf_count: int
    cap_count: int
    ceec_count: int
    primary_count: int
    junior_count: int
    senior_count: int
    paper_count: dict[str, int]


def _walk_small_dir(root: Path, skip_subtree: str | None = None) -> int:
    """Count ``*.pdf`` directly inside ``root`` (no recursion into siblings).

    Used only for ``cap_exam`` and ``ceec`` because the pre-built index
    excludes them. ``skip_subtree`` lets callers drop a sub-directory such as
    ``ceec/_generic`` (mirrors the legacy behaviour).
    """
    if not root.exists():
        return 0
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        parts = dirpath.split(os.sep)
        if skip_subtree and skip_subtree in parts:
            dirnames[:] = []
            continue
        for fname in filenames:
            if fname.endswith(".pdf"):
                total += 1
    return total


def _stats_from_local_index(archive_path: Path) -> tuple[int, int, int, dict[str, int]]:
    """Aggregate primary/junior/senior/paper counts from the cached index.

    Returns ``(pdf_total, primary, junior, senior, paper_count)``.

    The cached index excludes ``cap_exam``, ``ceec``, ``_inbox``, etc. — see
    ``app/scraper/local_index.py`` SKIP_TOP_DIRS. This is intentionally
    consistent with the legacy behaviour (those top-level dirs are also
    excluded from the level classification).

    Returns ``None`` if the index file does not exist.
    """
    index_path = archive_path / "state" / "local_papers_index.json"
    if not index_path.exists():
        return None

    try:
        data = json.loads(index_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None

    items = data.get("items") or []

    primary = 0
    junior = 0
    senior = 0
    paper_count = {k: 0 for k in LEVEL_PAPER_KEYS}
    pdf_total = 0

    for it in items:
        if it.get("ext") != "pdf":
            continue
        pdf_total += 1
        level = it.get("level", "")
        filetype = it.get("filetype", "")
        if level == "國小":
            primary += 1
            if filetype == "paper":
                paper_count["國小"] += 1
        elif level == "國中":
            junior += 1
            if filetype == "paper":
                paper_count["國中"] += 1
        elif level == "高中":
            senior += 1
            if filetype == "paper":
                paper_count["高中"] += 1

    return pdf_total, primary, junior, senior, paper_count


def _stats_from_filesystem_walk(archive_path: Path) -> PdfStats:
    """Legacy full-tree walk. Used only when the local index is missing."""
    pdf_count = 0
    cap_count = 0
    ceec_count = 0
    primary_count = 0
    junior_count = 0
    senior_count = 0
    paper_count: dict[str, int] = {k: 0 for k in LEVEL_PAPER_KEYS}

    for dirpath, dirnames, filenames in os.walk(archive_path):
        if Path(dirpath) == archive_path:
            dirnames[:] = [d for d in dirnames if d not in SKIP_TOP_DIRS]
            continue
        if "_generic" in dirpath.split(os.sep):
            dirnames[:] = []
            continue

        level_key: str | None = None
        for seg in dirpath.split(os.sep):
            if seg in LEVEL_PAPER_KEYS:
                level_key = seg
                break

        for fname in filenames:
            if not fname.endswith(".pdf"):
                continue
            pdf_count += 1
            rel = os.path.relpath(os.path.join(dirpath, fname), archive_path)
            rel_parts = rel.split(os.sep)
            top = rel_parts[0]
            if top == "cap_exam":
                cap_count += 1
                continue
            if top == "ceec":
                ceec_count += 1
                continue
            if level_key == "國小":
                primary_count += 1
                if "paper" in rel_parts:
                    paper_count["國小"] += 1
            elif level_key == "國中":
                junior_count += 1
                if "paper" in rel_parts:
                    paper_count["國中"] += 1
            elif level_key == "高中":
                senior_count += 1
                if "paper" in rel_parts:
                    paper_count["高中"] += 1

    return PdfStats(
        pdf_count=pdf_count,
        cap_count=cap_count,
        ceec_count=ceec_count,
        primary_count=primary_count,
        junior_count=junior_count,
        senior_count=senior_count,
        paper_count=paper_count,
    )


def collect_pdf_stats(archive_dir: Path | str) -> PdfStats:
    """Return PDF counts grouped by level.

    Strategy (2026-08-24 second pass):

    1. Try ``state/local_papers_index.json`` — covers 國小/國中/高中 + paper
       counts and the grand total of all PDFs in the index.
    2. Walk ``cap_exam/`` and ``ceec/`` separately (small subtrees, fast
       even on CIFS).
    3. Fall back to a full filesystem walk if the index is missing.

    The result mirrors the legacy 4-walk logic byte-for-byte, but the slow
    piece is replaced with a single 20MB JSON read.
    """
    archive_path = Path(archive_dir)

    indexed = _stats_from_local_index(archive_path)

    cap_count = _walk_small_dir(archive_path / "cap_exam")
    ceec_count = _walk_small_dir(archive_path / "ceec", skip_subtree="_generic")

    if indexed is not None:
        pdf_total, primary, junior, senior, paper_count = indexed
        # Index excludes cap_exam/ceec PDFs from the level classification, but
        # does include them in `items`. We want the grand total to include
        # them too, so add the small-dir counts back in.
        return PdfStats(
            pdf_count=pdf_total + cap_count + ceec_count,
            cap_count=cap_count,
            ceec_count=ceec_count,
            primary_count=primary,
            junior_count=junior,
            senior_count=senior,
            paper_count=paper_count,
        )

    # Fallback: full filesystem walk.
    return _stats_from_filesystem_walk(archive_path)


def render_summary(archive_dir: Path | str) -> str:
    """Render the human-readable status block used by ``tqark-archive-status``.

    Mirrors the legacy shell heredoc output so existing muscle memory keeps
    working; only the data-gathering path is faster.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    archive_path = Path(archive_dir)
    state_dir = archive_path / "state"
    log_dir = archive_path / "logs"
    status_file = state_dir / "archive_status.json"
    account_file = state_dir / "account_status.json"
    studyark_total_file = state_dir / "studyark_total.json"
    archive_log = log_dir / "archive.log"

    TZ_TAIPEI = ZoneInfo("Asia/Taipei")
    now_str = datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M:%S %Z")

    total = 0
    last_run = "never"
    last_result = "unknown"
    if status_file.exists():
        d = json.loads(status_file.read_text())
        total = d.get("total_collected", 0)
        last_run = d.get("last_run", "never")
        last_result = d.get("last_run_result", "unknown")

    studyark_total = 20158
    if studyark_total_file.exists():
        try:
            sd = json.loads(studyark_total_file.read_text())
            studyark_total = sd.get("last_count", 20158)
        except Exception:
            pass

    exhausted_with_recovery: list[tuple[str, int | None]] = []
    if account_file.exists():
        try:
            ad = json.loads(account_file.read_text())
            today = datetime.now(TZ_TAIPEI).date().isoformat()
            if "exhausted" in ad and isinstance(ad["exhausted"], list):
                exhausted_with_recovery = [(n, None) for n in ad["exhausted"]]
            elif ad.get("date") == today and "cooldown" in ad:
                for acc_name, until_str in ad["cooldown"].items():
                    try:
                        until = datetime.fromisoformat(until_str)
                        now = datetime.now(TZ_TAIPEI)
                        if now < until:
                            minutes_left = max(
                                0, int((until - now).total_seconds() / 60)
                            )
                            exhausted_with_recovery.append((acc_name, minutes_left))
                    except ValueError:
                        pass
        except Exception:
            pass

    stats = collect_pdf_stats(archive_path)

    pct = (total / studyark_total * 100) if studyark_total else 0
    remaining = studyark_total - total
    days_left = remaining / 120  # 4 accounts × 30/day

    lines: list[str] = []
    lines.append("═" * 55)
    lines.append("  TQark-web StudyArk Archive Status")
    lines.append(f"  {now_str}")
    lines.append("═" * 55)
    lines.append("")
    lines.append("📊 累計進度")
    lines.append(f"  已抓 fileids:    {total:,} / {studyark_total:,}  ({pct:.2f}%)")
    lines.append(f"  Disk 上 PDFs:    {stats['pdf_count']} 個")
    lines.append(
        f"  學段分布:        國小 {stats['paper_count']['國小']} / "
        f"國中 {stats['paper_count']['國中']} / 高中 {stats['paper_count']['高中']}"
    )
    lines.append("")
    lines.append("⏰ 預估完成 (4 帳號輪流)")
    lines.append("  每天抓取:        ~120 fileids/day")
    lines.append(f"  預估剩餘:        {remaining:,} 個 fileids")
    lines.append(f"  預估完成:        ~{days_left:.0f} 天 (~{int(days_left/30.4)} 個月)")
    lines.append("")
    lines.append("🕐 上次執行")
    lines.append(f"  時間:            {last_run}")
    lines.append(f"  結果:            {last_result}")
    lines.append("")

    lines.append("👥 帳號狀態")
    if exhausted_with_recovery:
        parts = []
        for name, mins in exhausted_with_recovery:
            if mins is None:
                parts.append(f"{name}")
            elif mins >= 60:
                parts.append(f"{name} ({mins // 60}h{mins % 60}m)")
            else:
                parts.append(f"{name} ({mins}m)")
        lines.append("  Exhausted:       " + ", ".join(parts))

        rec_times = []
        if account_file.exists():
            try:
                ad = json.loads(account_file.read_text())
                if ad.get("cooldown"):
                    for n, m in exhausted_with_recovery:
                        if m is not None and n in ad["cooldown"]:
                            rec_times.append((n, datetime.fromisoformat(ad["cooldown"][n])))
            except Exception:
                pass
        if rec_times:
            earliest = min(rec_times, key=lambda x: x[1])
            lines.append(
                f"  最早恢復:        {earliest[0]} @ {earliest[1].strftime('%H:%M:%S')}"
            )
    else:
        lines.append("  Exhausted:       (無 exhausted)")
    lines.append("")

    lines.append("📚 PDF 分布 (5 類)")
    lines.append(f"  📒 小學 (國小):  {stats['primary_count']}")
    lines.append(f"  📗 國中:        {stats['junior_count']}")
    lines.append(f"  📘 高中:        {stats['senior_count']}")
    lines.append(f"  📙 會考 (CAP):  {stats['cap_count']}")
    lines.append(f"  📕 大考 (CEEC): {stats['ceec_count']}")
    lines.append(
        f"  總計:           {stats['primary_count'] + stats['junior_count'] + stats['senior_count'] + stats['cap_count'] + stats['ceec_count']}"
    )
    lines.append("")
    lines.append("─" * 57)
    lines.append("📜 最近 5 批 batch")
    lines.append("─" * 57)
    if archive_log.exists():
        import subprocess

        try:
            tail = subprocess.check_output(
                ["grep", "-a", "Archive task done\\|rate_limited", str(archive_log)],
                stderr=subprocess.DEVNULL,
            ).decode(errors="replace")
            for line in tail.splitlines()[-5:]:
                lines.append(line)
        except subprocess.CalledProcessError:
            pass

    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "/mnt/my_book/考題收集"
    print(render_summary(target))
