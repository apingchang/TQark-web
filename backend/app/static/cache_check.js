// 【2026-08-17 新】外部 cache check script (避免 CSP script-src 'self' 阻擋)
// 載入後 2 秒若 yearSelect 還只有 1 option → 提示 user reload
(function () {
    window.addEventListener('load', function () {
        setTimeout(function () {
            var ys = document.getElementById('schoolYearSelect');
            if (ys && ys.options.length <= 1) {
                console.warn('[TQark] 學年 dropdown 沒有預設 options, 可能是 browser cache. 強制 reload Ctrl+Shift+R');
                // 在 UI 上也顯示提示 (banner)
                var banner = document.createElement('div');
                banner.style.cssText = 'position:fixed;top:10px;right:10px;background:#fff3cd;border:2px solid #856404;padding:15px;border-radius:5px;z-index:9999;max-width:400px;box-shadow:0 4px 8px rgba(0,0,0,0.2);';
                banner.innerHTML = '<strong>⚠️ 可能 Browser Cache</strong><br>學年下拉選單沒有選項, 請按 Ctrl+Shift+R 強制 reload.';
                document.body.appendChild(banner);
            }
        }, 2000);
    });
})();
