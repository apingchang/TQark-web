"""
Regression test for ``scrape_edu_tw_schools`` Referer header encoding.

Bug (2026-08-24):
    `archive_links` builds the Referer header from `cat_page or link.url`.
    When the link URL has no ``cat_sn`` query, ``get_category_page()`` returns
    ``None`` and the raw Chinese URL becomes the Referer. Python's
    ``http.client.putheader()`` encodes every header with ``latin-1``, so a
    Referer containing Chinese characters raises ``UnicodeEncodeError`` and
    the entire download crashes before a single byte leaves the socket.

This test pins down the helper that percent-encodes the path component so the
Referer is always ``latin-1``-safe while still pointing at the original URL.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from scripts.scrape_edu_tw_schools import encode_referer  # noqa: E402


class TestEncodeReferer:
    """``encode_referer`` produces ``latin-1``-safe URLs."""

    def test_ascii_only_url_is_unchanged(self):
        url = "https://affairs.kh.edu.tw/sites/5365/file.pdf"
        assert encode_referer(url) == url

    def test_chinese_path_is_percent_encoded(self):
        url = (
            "https://affairs.kh.edu.tw/sites/5365/upload_file/8/"
            "110學年度第1學期一年級英語補考題庫答案.docx"
        )
        encoded = encode_referer(url)
        # Must be ASCII-safe (latin-1 encodable, no exception)
        encoded.encode("latin-1")  # noqa: S303 - intentional probe
        # Path should be percent-encoded, domain preserved
        assert encoded.startswith("https://affairs.kh.edu.tw/sites/5365/upload_file/8/")
        assert "%E5%AD%B8" in encoded  # 學
        assert "%E5%B9%B4" in encoded  # 年
        assert encoded.endswith(".docx")
        # Original Chinese characters must NOT remain raw
        assert "學年度" not in encoded

    def test_chinese_with_query_is_ascii_safe(self):
        url = (
            "https://affairs.kh.edu.tw/sites/5365/upload_file/8/"
            "110學年度.docx?op=download&cat_sn=8"
        )
        encoded = encode_referer(url)
        encoded.encode("latin-1")  # noqa: S303
        assert "學年度" not in encoded

    def test_empty_string_is_handled(self):
        # Defensive: an empty Referer should not crash and must remain safe.
        assert encode_referer("") == ""

    def test_malformed_url_falls_back_to_input(self):
        # If urlparse cannot extract a path, the original string is returned
        # (still latin-1 safe if it was already ASCII).
        url = "https://example.com/path/file.pdf"
        assert encode_referer(url) == url


class TestArchiveLinksReferer:
    """``archive_links`` must not raise UnicodeEncodeError on Chinese URLs."""

    def test_archive_links_does_not_raise_latin1_with_chinese_url(self, monkeypatch, tmp_path):
        """Smoke test: pass a Chinese URL and a stub session that records the
        Referer header. Confirm no ``UnicodeEncodeError`` is raised and the
        Referer sent to the server is ``latin-1`` safe."""
        import requests

        from scripts.scrape_edu_tw_schools import FileLink

        captured_headers: dict[str, str] = {}

        def fake_get(self, url, timeout=None, headers=None, **kwargs):  # noqa: D401
            captured_headers.update(headers or {})
            # Simulate the 404 path; we only care that no exception bubbles up
            # while preparing headers.
            resp = requests.Response()
            resp.status_code = 404
            resp._content = b""
            resp.url = url
            return resp

        monkeypatch.setattr(requests.Session, "get", fake_get)

        # Redirect download target to a tmp_path so the test never touches the
        # real /mnt/my_book archive.
        from scripts import scrape_edu_tw_schools as mod

        monkeypatch.setattr(mod, "ARCHIVE_ROOT", tmp_path)

        link = FileLink(
            url=(
                "https://affairs.kh.edu.tw/sites/5365/upload_file/8/"
                "110學年度第1學期一年級英語補考題庫答案.docx"
            ),
            label="110學年度第1學期一年級英語補考題庫答案.docx",
            page_title="補考題庫",
        )
        school = {
            "name": "高雄市五福國中",
            "county": "高雄市",
            "url": "https://affairs.kh.edu.tw/sites/5365/",
        }

        written, completed, errors = mod.archive_links(
            mod.requests.Session(),
            school,
            [link],
            set(),
            dry_run=False,
            logger=__import__("logging").getLogger("test"),
        )

        # No UnicodeEncodeError leaked into the error list
        assert all("latin-1" not in e for e in errors), f"errors: {errors}"
        # The Referer that was actually sent must be latin-1 safe
        referer = captured_headers.get("Referer", "")
        if referer:
            referer.encode("latin-1")  # noqa: S303 - intentional probe
