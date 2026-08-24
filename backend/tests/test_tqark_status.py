"""
Regression test for ``scripts.tqark_status.collect_pdf_stats``.

Background (2026-08-24):
    The legacy ``tqark-archive-status`` shell script walked the entire
    ``/mnt/my_book/考題收集`` tree 4 times: once with ``os.walk`` and 3 more
    times via ``find`` subprocesses. On the CIFS mount each tree walk took
    minutes, so the whole status report stalled 10-20 minutes.

This test pins down the consolidated single-walk implementation:

    >>> collect_pdf_stats(Path("/some/archive"))

returns the same numbers the legacy 4-walk logic produced, but in one pass.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from scripts.tqark_status import collect_pdf_stats  # noqa: E402


@pytest.fixture
def fake_archive(tmp_path: Path) -> Path:
    """Build a synthetic archive tree covering every classification branch."""
    # Top-level dirs that MUST be skipped (state/logs/_inbox/_internal/...)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "irrelevant.pdf").write_bytes(b"%PDF-fake")
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "ignored.pdf").write_bytes(b"%PDF-fake")
    (tmp_path / "_inbox").mkdir()
    (tmp_path / "_inbox" / "skipped.pdf").write_bytes(b"%PDF-fake")
    (tmp_path / "_internal").mkdir()
    (tmp_path / "_internal" / "skipped.pdf").write_bytes(b"%PDF-fake")
    (tmp_path / "_未分類").mkdir()
    (tmp_path / "_未分類" / "skipped.pdf").write_bytes(b"%PDF-fake")
    (tmp_path / "其他X").mkdir()
    (tmp_path / "其他X" / "skipped.pdf").write_bytes(b"%PDF-fake")
    (tmp_path / "未分類").mkdir()
    (tmp_path / "未分類" / "skipped.pdf").write_bytes(b"%PDF-fake")

    # cap_exam → cap_count
    (tmp_path / "cap_exam").mkdir()
    (tmp_path / "cap_exam" / "cap1.pdf").write_bytes(b"%PDF-fake")
    (tmp_path / "cap_exam" / "cap2.pdf").write_bytes(b"%PDF-fake")

    # ceec → ceec_count
    (tmp_path / "ceec").mkdir()
    (tmp_path / "ceec" / "ceec1.pdf").write_bytes(b"%PDF-fake")

    # ceec/_generic must NOT contribute to ceec_count (and subtree is skipped)
    (tmp_path / "ceec" / "_generic").mkdir()
    (tmp_path / "ceec" / "_generic" / "should_not_count.pdf").write_bytes(b"%PDF-fake")
    # Also a sibling of _generic: still counts (only the _generic subtree is excluded)
    (tmp_path / "ceec" / "sibling").mkdir()
    (tmp_path / "ceec" / "sibling" / "ceec2.pdf").write_bytes(b"%PDF-fake")

    # 國小/紙本/paper (with paper)
    (tmp_path / "高雄市" / "國小" / "三年級" / "數學" / "paper").mkdir(parents=True)
    (tmp_path / "高雄市" / "國小" / "三年級" / "數學" / "paper" / "p1.pdf").write_bytes(b"%PDF-fake")
    # 國小 without paper (daan)
    (tmp_path / "高雄市" / "國小" / "三年級" / "數學" / "daan").mkdir(parents=True)
    (tmp_path / "高雄市" / "國小" / "三年級" / "數學" / "daan" / "d1.pdf").write_bytes(b"%PDF-fake")

    # 國中/紙本/paper + 國中/紙本/daan
    (tmp_path / "臺北市" / "國中" / "八年級" / "英語" / "paper").mkdir(parents=True)
    (tmp_path / "臺北市" / "國中" / "八年級" / "英語" / "paper" / "p2.pdf").write_bytes(b"%PDF-fake")
    (tmp_path / "臺北市" / "國中" / "八年級" / "英語" / "paper" / "p3.pdf").write_bytes(b"%PDF-fake")
    (tmp_path / "臺北市" / "國中" / "八年級" / "英語" / "daan").mkdir(parents=True)
    (tmp_path / "臺北市" / "國中" / "八年級" / "英語" / "daan" / "d2.pdf").write_bytes(b"%PDF-fake")

    # 高中/紙本/paper
    (tmp_path / "新北市" / "高中" / "十年級" / "物理" / "paper").mkdir(parents=True)
    (tmp_path / "新北市" / "高中" / "十年級" / "物理" / "paper" / "p4.pdf").write_bytes(b"%PDF-fake")

    # A non-PDF that should be ignored
    notes_dir = tmp_path / "高雄市" / "國中" / "七年級" / "國文" / "paper"
    notes_dir.mkdir(parents=True, exist_ok=True)
    (notes_dir / "notes.txt").write_text("not a pdf")

    # A PDF whose path contains 'paper' but NOT as a level — should not bump paper_count
    (tmp_path / "桃園市" / "國中" / "七年級" / "paper_handout" / "x.pdf")  # dir not named 'paper'
    (tmp_path / "桃園市" / "國中" / "七年級" / "paper_handout").mkdir(parents=True)
    (tmp_path / "桃園市" / "國中" / "七年級" / "paper_handout" / "x.pdf").write_bytes(b"%PDF-fake")
    # The above is in 國中, NOT in /paper/, so paper_count[國中] stays the same.

    return tmp_path


class TestCollectPdfStats:
    def test_returns_expected_counts(self, fake_archive: Path):
        stats = collect_pdf_stats(fake_archive)

        # PDF counts (excluding skipped dirs, non-pdfs)
        assert stats["pdf_count"] == 11
        # 5-class classification
        assert stats["cap_count"] == 2
        assert stats["ceec_count"] == 2   # ceec1 + ceec/sibling/ceec2
        assert stats["primary_count"] == 2  # 國小 p1 + d1
        assert stats["junior_count"] == 4   # 國中 p2, p3, d2, paper_handout/x
        assert stats["senior_count"] == 1

        # paper_count: only PDFs under a /paper/ directory
        assert stats["paper_count"] == {
            "國小": 1,  # 國小/paper/p1
            "國中": 2,  # 國中/paper/p2 + p3
            "高中": 1,  # 高中/paper/p4
        }

    def test_skips_top_level_dirs(self, fake_archive: Path):
        """state/logs/_inbox/_internal/_未分類/其他X/未分類 must not contribute."""
        stats = collect_pdf_stats(fake_archive)
        # Each of those top-level dirs holds one fake .pdf that must be ignored.
        # If any slipped through, pdf_count would be 11 + 6 = 17.
        assert stats["pdf_count"] == 11

    def test_ceec_generic_subtree_excluded(self, fake_archive: Path):
        """Files under ceec/_generic must not be counted (matches web UI)."""
        stats = collect_pdf_stats(fake_archive)
        # ceec_count would be 3 if _generic leaked through.
        assert stats["ceec_count"] == 2

    def test_no_find_subprocess_used(self, monkeypatch, fake_archive: Path):
        """The consolidated implementation must NOT shell out to ``find``.

        The legacy code ran ``find`` 3 times. If we regress and re-introduce
        those subprocess calls, this test fails.
        """
        import subprocess

        calls: list[list[str]] = []
        real_check_output = subprocess.check_output

        def spy_check_output(*args, **kwargs):
            calls.append(args[0])
            return real_check_output(*args, **kwargs)

        monkeypatch.setattr(subprocess, "check_output", spy_check_output)

        collect_pdf_stats(fake_archive)

        find_calls = [c for c in calls if c and c[0] == "find"]
        assert find_calls == [], f"find subprocess should not run, got: {find_calls}"

    def test_empty_archive_returns_zeros(self, tmp_path: Path):
        stats = collect_pdf_stats(tmp_path)
        assert stats["pdf_count"] == 0
        assert stats["cap_count"] == 0
        assert stats["ceec_count"] == 0
        assert stats["primary_count"] == 0
        assert stats["junior_count"] == 0
        assert stats["senior_count"] == 0
        assert stats["paper_count"] == {"國小": 0, "國中": 0, "高中": 0}

    def test_single_walk_completes_quickly(self, fake_archive: Path):
        """Smoke test: a single walk over a synthetic tree should be fast.

        We assert <2s here. The real archive takes much longer because of
        CIFS, but the unit-level cost is dominated by Python work, not I/O.
        """
        import time

        start = time.monotonic()
        collect_pdf_stats(fake_archive)
        elapsed = time.monotonic() - start
        assert elapsed < 2.0, f"collect_pdf_stats too slow: {elapsed:.2f}s"
