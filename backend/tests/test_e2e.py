"""
Playwright E2E tests for TQark-web (2026-08-17 新).

涵蓋 dashboard + search + 統一考試 的 end-to-end user flows.

不取代 pytest 現有 unit tests, 是補充 — 用真實 Chromium browser 模擬 user click.

使用方式:
    cd backend && .venv/bin/python -m pytest tests/test_e2e.py -v --browserplaywright
    或
    cd backend && .venv/bin/python -m pytest tests/test_e2e.py -v

需求:
    playwright (chromium-1148 已下載)
"""
import asyncio
import time

import pytest
from playwright.sync_api import sync_playwright, expect

# ====================
# Fixtures
# ====================
BASE_URL = "http://127.0.0.1:8000"


import pytest
from playwright.sync_api import sync_playwright, expect


@pytest.fixture(scope="module")
def browser():
    """Playwright Chromium browser (headless)."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture
def logged_in_page(browser, admin_token):
    """已登入的 page (tqark_session cookie).

    admin_token fixture 來自 conftest.py (session-scope, 整個 session 只拿一次).
    """
    context = browser.new_context()
    context.add_cookies([{
        "name": "tqark_session",
        "value": admin_token,
        "domain": "127.0.0.1",
        "path": "/",
    }])
    page = context.new_page()
    yield page
    context.close()


# ====================
# Tests
# ====================
class TestDashboardLayout:
    """Dashboard form layout - 縣市放左上, cascading dropdowns"""

    def test_county_is_first_filter(self, logged_in_page):
        """Dashboard 第一個 select 應該是縣市 (county)"""
        logged_in_page.goto(f"{BASE_URL}/dashboard")
        # 找第一個 <select> element
        first_select = logged_in_page.query_selector("select")
        assert first_select is not None, "no select found on dashboard"
        first_select_id = first_select.get_attribute("id")
        assert first_select_id == "countySelect", (
            f"第一個 select 應該是 countySelect, 實際是 {first_select_id}"
        )

    def test_school_dropdown_disabled_until_county_selected(self, logged_in_page):
        """學校 dropdown 應該 disable 直到選 county"""
        logged_in_page.goto(f"{BASE_URL}/dashboard")
        school = logged_in_page.locator("#schoolNameSelect")
        assert school.is_disabled(), "學校 dropdown 應該 disabled (沒選 county)"

    def test_selecting_county_enables_school_dropdown(self, logged_in_page):
        """選 county 後, 學校 dropdown 應該 enabled 並列出該縣市學校"""
        page = logged_in_page
        page.goto(f"{BASE_URL}/dashboard")

        # 選 高雄市
        page.select_option("#countySelect", "高雄市")

        # 等 JS 載入
        page.wait_for_function("!document.getElementById('schoolNameSelect').disabled", timeout=5000)

        school = page.locator("#schoolNameSelect")
        assert not school.is_disabled(), "選 county 後 學校應該 enabled"

        # 學校 options 應該包含楠梓國中
        options = page.eval_on_selector_all(
            "#schoolNameSelect option",
            "opts => opts.map(o => o.value)"
        )
        assert any("楠梓" in o for o in options), (
            f"學校清單應該含楠梓國中, 實際: {options[:10]}"
        )


class TestSearchResults:
    """/ui/search 結果頁 chips 跟 fallback UX"""

    def test_search_with_county_and_school_returns_results(self, logged_in_page):
        """county + school 搜尋應該有結果"""
        page = logged_in_page
        page.goto(f"{BASE_URL}/ui/search?county=高雄市&school_name=高雄市楠梓國中")

        # 應該有「共 N 組」文字
        content = page.content()
        import re
        m = re.search(r"共\s*<strong>(\d+)</strong>\s*組", content)
        assert m is not None, "找不到「共 N 組」"
        total = int(m.group(1))
        assert total > 0, f"應該有結果, 實際 {total}"

    def test_filter_chips_display_all_conditions(self, logged_in_page):
        """Filter chips 應該顯示所有 active filter"""
        page = logged_in_page
        page.goto(f"{BASE_URL}/ui/search?county=臺北市&school_name=中山國中&grade=七年級&school_year=110")

        content = page.content()
        # 應該有 chip 顯示「縣市: 臺北市」
        assert "縣市:" in content, "缺少縣市 chip"
        # 應該有「年級: 七年級」
        assert "年級:" in content or "七年級" in content, "缺少年級 chip"
        # 應該有「學年: 110」
        assert "學年:" in content or "110" in content, "缺少學年 chip"
        # 應該有「學校關鍵字: 中山國中」
        assert "學校關鍵字:" in content or "中山國中" in content, "缺少學校 chip"

    def test_remove_filter_chip_works(self, logged_in_page):
        """點 chip 的 × 應該移除那個 filter"""
        page = logged_in_page
        page.goto(f"{BASE_URL}/ui/search?county=臺北市&school_year=110")

        # 找學年 chip 的 × (移除學年)
        # chip 是 <span class="badge bg-success me-2">學年: 110 <a href="..." class="...×">×</a></span>
        # 找有「學年: 110」的那個 badge 然後裡面的 ×
        # 簡單做法: 找所有「×」link, 找其中 href 不含 school_year 的第一個
        chip_links = page.locator("a[title='移除此條件']")
        count_before = chip_links.count()
        assert count_before >= 2, f"應該有 ≥ 2 個 chip ×, 實際 {count_before}"

        # 點第一個 × (理論上會移除 county 或 school_year 中的一個)
        first_link = chip_links.first
        href = first_link.get_attribute("href")
        # 點 link
        first_link.click()
        page.wait_for_load_state("networkidle")

        # URL 應該已經改變
        new_url = page.url
        assert new_url != f"{BASE_URL}/ui/search?county=臺北市&school_year=110", (
            f"點 × 後 URL 應該改變, 但仍是 {new_url}"
        )


class TestUnifiedExam:
    """統一考試 (會考/大學入學考) 的 E2E 行為"""

    def test_cap_exam_shows_cap_template_and_chips(self, logged_in_page):
        """選 grade=會考 → 顯示 CAP template + chips"""
        page = logged_in_page
        page.goto(f"{BASE_URL}/ui/search?grade=會考")

        # 應該有「歷屆會考」title
        title = page.title()
        assert "會考" in title, f"title 應該含會考, 實際 {title}"

        # 應該有「考試類別: 會考」chip
        content = page.content()
        assert "考試類別:" in content, "缺少考試類別 chip"
        assert "會考" in content, "缺少會考"

    def test_cap_exam_year_filter(self, logged_in_page):
        """會考 + year filter 應該 work"""
        page = logged_in_page
        page.goto(f"{BASE_URL}/ui/search?grade=會考&school_year=110")

        # 應該有「學年: 110」chip
        content = page.content()
        assert "學年:" in content, "缺少學年 chip"
        assert "110" in content, "缺少 110"

    def test_ceec_exam_shows_ceec_template(self, logged_in_page):
        """選 grade=大學入學考 → 顯示 CEEC template"""
        page = logged_in_page
        page.goto(f"{BASE_URL}/ui/search?grade=大學入學考")

        title = page.title()
        assert "大學入學考" in title or "CEEC" in title, (
            f"title 應該含大學入學考/CEEC, 實際 {title}"
        )

        content = page.content()
        assert "CEEC" in content or "大學入學考" in content, "缺少 CEEC"


class TestGradeCascading:
    """Grade dropdown cascading - 選學校後只列該校年級 + 統一考試保留"""

    def test_dashboard_loads_with_all_grade_options(self, logged_in_page):
        """Dashboard 載入時 grade dropdown 應該有國小/國中/高中/統一考試"""
        page = logged_in_page
        page.goto(f"{BASE_URL}/dashboard")

        # 找 grade optgroup
        content = page.content()
        assert 'optgroup label="國小"' in content, "缺少國小 optgroup"
        assert 'optgroup label="國中"' in content, "缺少國中 optgroup"
        assert 'optgroup label="高中"' in content, "缺少高中 optgroup"
        # 統一考試永遠保留
        assert 'value="會考"' in content, "缺少會考 option"
        assert 'value="大學入學考"' in content, "缺少大學入學考 option"

    def test_selecting_junior_school_hides_high_school_grade(self, logged_in_page):
        """選國中學校後, grade dropdown 應該隱藏「高中」optgroup"""
        page = logged_in_page
        page.goto(f"{BASE_URL}/dashboard")

        # 選臺北市 + 中山國中
        page.select_option("#countySelect", "臺北市")
        page.wait_for_function(
            "!document.getElementById('schoolNameSelect').disabled",
            timeout=5000
        )

        # 選 中山國中
        page.select_option("#schoolNameSelect", "臺北市立中山國中")

        # 等 JS cascading 完成 (gradeOptions 函式 trigger)
        page.wait_for_timeout(500)

        # 看 gradeOptgroupSenior 是否 hidden
        is_hidden = page.evaluate(
            "() => document.getElementById('gradeOptgroupSenior').hidden"
        )
        assert is_hidden is True, "高中 optgroup 應該隱藏 (中山國中是國中)"

        # 統一考試 optgroup 不應該隱藏
        # (找「統一考試」字樣)
        content = page.content()
        assert "會考" in content, "統一考試 (會考) 應該永遠保留"
        assert "大學入學考" in content, "統一考試 (大學入學考) 應該永遠保留"


class TestUnifiedExamDashboard:
    """統一考試 (會考/大考) dashboard 行為"""

    def test_selecting_cap_keeps_subject_visible(self, logged_in_page):
        """選會考時, 科目 dropdown 應該保持可見且有 CAP 真實科目"""
        page = logged_in_page
        page.goto(f"{BASE_URL}/dashboard")

        # 選會考
        page.select_option("#gradeSelect", "會考")
        page.wait_for_timeout(300)  # 等 JS setModeCap 完成

        # 科目 dropdown 應該 visible
        subject = page.locator("#subjectSelect")
        assert subject.is_visible(), "CAP 模式下 科目 dropdown 應該 visible"
        assert not subject.is_disabled(), "CAP 模式下 科目 dropdown 應該 enabled"

        # 科目 options 應該是 CAP 真實科目 (從 window.DASHBOARD_SUBJECTS)
        cap_subjects = page.evaluate("() => window.DASHBOARD_SUBJECTS?.cap || []")
        assert len(cap_subjects) > 0, f"應該有 CAP 科目, 實際 {cap_subjects}"

    def test_selecting_ceec_keeps_subject_visible(self, logged_in_page):
        """選大考時, 科目 dropdown 應該保持可見且有 CEEC 真實科目"""
        page = logged_in_page
        page.goto(f"{BASE_URL}/dashboard")

        page.select_option("#gradeSelect", "大學入學考")
        page.wait_for_timeout(300)

        subject = page.locator("#subjectSelect")
        assert subject.is_visible(), "CEEC 模式下 科目 dropdown 應該 visible"
        assert not subject.is_disabled(), "CEEC 模式下 科目 dropdown 應該 enabled"

        ceec_subjects = page.evaluate("() => window.DASHBOARD_SUBJECTS?.ceec || []")
        assert len(ceec_subjects) > 0, f"應該有 CEEC 科目, 實際 {ceec_subjects}"

    def test_cap_mode_shows_cap_page_title(self, logged_in_page):
        """CAP 模式時, page title 應該顯示「會考考題搜尋」"""
        page = logged_in_page
        page.goto(f"{BASE_URL}/dashboard")

        page.select_option("#gradeSelect", "會考")
        page.wait_for_timeout(300)

        title = page.locator("#pageTitle").text_content()
        assert "會考" in title, f"page title 應該含會考, 實際 '{title}'"

    def test_ceec_mode_shows_ceec_page_title(self, logged_in_page):
        """CEEC 模式時, page title 應該顯示「大考考題搜尋」"""
        page = logged_in_page
        page.goto(f"{BASE_URL}/dashboard")

        page.select_option("#gradeSelect", "大學入學考")
        page.wait_for_timeout(300)

        title = page.locator("#pageTitle").text_content()
        assert "大考" in title or "大學入學考" in title, (
            f"page title 應該含大考/大學入學考, 實際 '{title}'"
        )

    def test_cap_year_select_dropdown_loaded(self, logged_in_page):
        """CAP 模式時, 學年 select dropdown 應該填 CAP 真實年度"""
        page = logged_in_page
        page.goto(f"{BASE_URL}/dashboard")

        page.select_option("#gradeSelect", "會考")
        page.wait_for_timeout(300)

        # 【2026-08-17 改】學年從 datalist 改成 select dropdown
        year_count = page.locator("#schoolYearSelect option").count()
        assert year_count > 1, f"CAP 模式 schoolYearSelect 應該有 options (不限 + N 個年度), 實際 {year_count}"
