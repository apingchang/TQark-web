// =========================================================
// 首頁新聞摘要輪播 (懂王/靈覓)
// 【2026-07-26 William 改】
// - 上半 (懂王): 30 秒輪播
// - 下半 (靈覓): 等上半部 5 秒 才切換輪播的次頁
//   → 5s initial delay, then 30s rotation (offset 5s from trump)
// =========================================================
const TRUMP_ROTATE_MS = 30000;     // 上半 (懂王) 週期
const LINGMIAN_DELAY_MS = 5000;    // 下半 (靈覓) 起始延遲
const LINGMIAN_ROTATE_MS = 30000;  // 下半 (靈覓) 週期 (跟 trump 同步, 只是 offset 5s)
const NEWS_PER_PAGE = 3;

// 從 server 注入的 JSON data 拿 news
function loadNewsData() {
    const el = document.getElementById('news-data');
    if (!el) return { lingmian: [], trump: [] };
    try {
        return JSON.parse(el.textContent);
    } catch (e) {
        console.error('Failed to parse news data:', e);
        return { lingmian: [], trump: [] };
    }
}

const newsData = loadNewsData();

function escapeHtml(s) {
    if (!s) return '';
    return s.replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
}

function truncate(s, n) {
    if (!s) return '';
    return s.length > n ? s.slice(0, n) + '…' : s;
}

function renderNewsItems(items) {
    if (!items || items.length === 0) {
        return '<p class="text-muted text-center my-4">尚無今日新聞摘要</p>';
    }
    return items.map(item => `
        <div class="news-item">
            <a href="${escapeHtml(item.url)}" target="_blank" rel="noopener" class="news-title">
                🔶 ${escapeHtml(truncate(item.title, 120))}
            </a>
            ${item.summary ? `<div class="news-summary">${escapeHtml(truncate(item.summary, 280))}</div>` : ''}
            <div class="news-meta">
                ${item.category ? `<span class="badge bg-light text-dark">${escapeHtml(item.category)}</span>` : ''}
                ${item.source ? `<span class="ms-2">${escapeHtml(item.source)}</span>` : ''}
            </div>
        </div>
    `).join('');
}

function setupSource(sourceId, rotateMs, initialDelayMs) {
    initialDelayMs = initialDelayMs || 0;
    const body = document.getElementById(sourceId + 'Body');
    if (!body) return;
    const allItems = newsData[sourceId] || [];
    const totalPages = Math.max(1, Math.ceil(allItems.length / NEWS_PER_PAGE));
    let currentPage = 0;

    function render() {
        const start = currentPage * NEWS_PER_PAGE;
        const pageItems = allItems.slice(start, start + NEWS_PER_PAGE);
        body.innerHTML = renderNewsItems(pageItems);
        const indicator = document.querySelector('.' + sourceId + '-page-indicator');
        if (indicator) indicator.textContent = currentPage + 1;
        const totalEl = document.querySelector('.' + sourceId + '-total-pages');
        if (totalEl) totalEl.textContent = totalPages;
    }

    function next() {
        currentPage = (currentPage + 1) % totalPages;
        render();
    }
    function prev() {
        currentPage = (currentPage - 1 + totalPages) % totalPages;
        render();
    }

    const nextBtn = document.querySelector('.' + sourceId + '-next');
    const prevBtn = document.querySelector('.' + sourceId + '-prev');
    if (nextBtn) nextBtn.addEventListener('click', next);
    if (prevBtn) prevBtn.addEventListener('click', prev);

    // 自動輪播: 起始延遲後啟動週期
    function startRotation() {
        setInterval(next, rotateMs);
    }
    if (initialDelayMs > 0) {
        setTimeout(startRotation, initialDelayMs);
    } else {
        startRotation();
    }
}

// Init
document.addEventListener('DOMContentLoaded', () => {
    // 上半 (懂王): 30 秒輪播, 立即啟動
    setupSource('trump', TRUMP_ROTATE_MS, 0);
    // 下半 (靈覓): 30 秒週期, 起始延遲 5 秒
    setupSource('lingmian', LINGMIAN_ROTATE_MS, LINGMIAN_DELAY_MS);
});
