// 全頁面 tooltip init (navbar user info hover 等)
// 必須用外部檔,CSP script-src 'self' 不允許 inline
document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('[data-bs-toggle="tooltip"][data-tooltip-lines]').forEach(el => {
        let lines = [];
        try { lines = JSON.parse(el.dataset.tooltipLines); } catch(e) { console.error('tooltip lines parse failed', e); }
        const html = '<div style="text-align:left;line-height:1.5">' + lines.join('<br>') + '</div>';
        new bootstrap.Tooltip(el, {title: html, html: true, sanitize: false});
    });
});