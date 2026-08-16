"""
Integration tests for /ui/search and /ui/download/paper/{paper_id}

用 FastAPI TestClient + 真實 admin token (in-process)
驗證 route 邏輯（從 in-process app，不是真的 HTTP server）
"""

import sys
import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.main import app  # noqa: E402
from app.db.session import AsyncSessionLocal  # noqa: E402
from app.core.security import create_access_token  # noqa: E402
from app.scraper import local_index  # noqa: E402


@pytest.fixture(scope="module")
def admin_token():
    """拿 admin user 的 JWT token"""
    async def _get():
        async with AsyncSessionLocal() as db:
            r = await db.execute(text(
                "SELECT id FROM users WHERE email='apingchang@gmail.com'"
            ))
            uid = r.scalar()
            return create_access_token({"uid": str(uid), "email": "apingchang@gmail.com"})
    return asyncio.run(_get())


@pytest.fixture
def client(admin_token):
    """FastAPI TestClient + admin cookie"""
    c = TestClient(app)
    c.cookies.set("tqark_session", admin_token)
    return c


class TestUiSearch:
    """測 /ui/search route"""

    def test_unauth_returns_401(self, client):
        """未登入 → 401 (TestClient 不帶 cookie)"""
        from fastapi.testclient import TestClient
        anon = TestClient(app)
        r = anon.get("/ui/search", params={"county": "高雄市"})
        # 因為 require_approved 會 raise HTTPException 401
        assert r.status_code in (401, 307)  # 401 or redirect

    def test_search_nanzi_returns_results(self, client):
        """William 之前抱怨的 0 results case — 應該至少 1 result"""
        r = client.get("/ui/search", params={
            "county": "高雄市",
            "school_name": "高雄市楠梓國中",
        })
        assert r.status_code == 200
        # 至少要有 download links
        import re
        paper_ids = re.findall(r"/ui/download/paper/([a-f0-9]+)", r.text)
        assert len(paper_ids) > 0, "高雄市+楠梓 應該有 results, 但 0"

    def test_search_partial_school_name(self, client):
        """partial school_name match（dashboard dropdown value）"""
        r = client.get("/ui/search", params={
            "county": "高雄市",
            "school_name": "楠梓",  # partial
        })
        import re
        paper_ids = re.findall(r"/ui/download/paper/([a-f0-9]+)", r.text)
        assert len(paper_ids) > 0

    # 【2026-08-16 改】_inbox 不進 search, 這個 test 移除
    # def test_search_inbox_school_lingya: 已不適用

    def test_search_grade_subject(self, client):
        """年級+科目 filter"""
        r = client.get("/ui/search", params={
            "grade": "七年級",
            "subject": "數學",
        })
        import re
        paper_ids = re.findall(r"/ui/download/paper/([a-f0-9]+)", r.text)
        # 可能 0，但 200 + 沒錯誤是基本
        assert r.status_code == 200

    def test_search_no_results_shows_message(self, client):
        """0 results → 顯示「沒有結果」訊息"""
        r = client.get("/ui/search", params={
            "county": "不存在縣市",
            "grade": "不存在年級",
        })
        assert r.status_code == 200
        assert "沒有結果" in r.text or "試試放寬條件" in r.text

    def test_search_no_studyark_rate_limit_message(self, client):
        """【2026-08-15 改】不該再看到 StudyArk 限流 alert"""
        r = client.get("/ui/search", params={"county": "高雄市"})
        assert "StudyArk 限流" not in r.text

    def test_cap_exam_routing(self, client):
        """grade=會考 應該走 CAP route 而不是 local_index"""
        r = client.get("/ui/search", params={"grade": "會考"})
        assert r.status_code == 200

    def test_ceec_exam_routing(self, client):
        """grade=大學入學考 → CEEC route"""
        r = client.get("/ui/search", params={"grade": "大學入學考"})
        assert r.status_code == 200

    def test_pagination_links(self, client):
        """夠多 results 時應該有分頁連結"""
        r = client.get("/ui/search", params={"per_page": 8}, cookies=client.cookies)
        # 找到 /ui/search?page= 連結
        import re
        page_links = re.findall(r"page=(\d+)", r.text)
        # 如果 total > 8 就有 page links
        if "上一頁" in r.text or "下一頁" in r.text:
            assert len(page_links) > 0


class TestUiDownloadPaper:
    """測 /ui/download/paper/{paper_id} route"""

    def test_download_paper_returns_pdf(self, client):
        """下載 paper → 200 + application/pdf + %PDF magic"""
        # 先找一個 valid paper_id
        items = local_index.get_index()
        for it in items:
            if it["filetype"] == "paper" and it["abs_path"]:
                p = Path(it["abs_path"])
                if p.exists():
                    target_id = it["paper_id"]
                    break
        else:
            pytest.skip("no valid paper item")

        r = client.get(f"/ui/download/paper/{target_id}", params={"filetype": "paper"})
        assert r.status_code == 200
        assert r.headers.get("content-type", "").startswith("application/pdf")
        assert r.content[:4] == b"%PDF"

    def test_download_daan_fallback(self, client):
        """當 group 只有 daan 時, filetype=paper 應該 fallback 到 daan"""
        # 找一個 group 只有 daan 的
        items = local_index.get_index()
        groups_cache = local_index._groups_cache
        for gk, gitems in groups_cache.items():
            paper_items = [it for it in gitems if it["filetype"] == "paper"]
            daan_items = [it for it in gitems if it["filetype"] == "daan"]
            if daan_items and not paper_items:
                # 這個 group 只有 daan
                target_id = daan_items[0]["paper_id"]
                r = client.get(f"/ui/download/paper/{target_id}", params={"filetype": "paper"})
                assert r.status_code == 200, (
                    f"daan-only group fallback failed for paper_id={target_id}"
                )
                assert r.content[:4] == b"%PDF"
                return
        pytest.skip("no daan-only group in index")

    def test_download_unknown_id_returns_404(self, client):
        r = client.get("/ui/download/paper/ffffffffffffffff", params={"filetype": "paper"})
        assert r.status_code == 404


class TestBuildMtime:
    """測右欄顯示的 build_mtime"""

    def test_dashboard_shows_build_mtime(self, client):
        r = client.get("/dashboard")
        import re
        m = re.search(r"程式最後修改: <code>([^<]+)</code>", r.text)
        assert m is not None, "build_mtime 沒在 dashboard 顯示"
        # 格式: YYYY-MM-DD HH:MM:SS
        assert re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", m.group(1))

    def test_search_page_shows_build_mtime(self, client):
        r = client.get("/ui/search", params={"county": "高雄市"})
        import re
        m = re.search(r"程式最後修改: <code>([^<]+)</code>", r.text)
        assert m is not None


class TestDashboardSubmitButton:
    """測 dashboard form 有 submit button"""

    def test_dashboard_has_submit_button(self, client):
        r = client.get("/dashboard")
        # form 內有 type="submit" button
        import re
        # 找 form[action="/ui/search"] 內的 submit
        form_match = re.search(
            r'<form[^>]*action="/ui/search"[^>]*>(.*?)</form>',
            r.text,
            re.DOTALL,
        )
        assert form_match is not None
        assert 'type="submit"' in form_match.group(1)
        assert "🔍 搜尋" in form_match.group(1)