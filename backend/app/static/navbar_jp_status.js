// === 【2026-08-01 新】Navbar 翻譯狀態指示器 ===
//
// - Poll /api/tools/jp-status 每 3 秒
// - 根據 state 顯示不同 badge, click → /tools?tool=jp
// - idle state 隱藏 indicator
// - 完成後 30 秒自動隱藏 (避免一直卡著)
//
// state 文案對照:
//   - queued  → ⏳ 排隊中
//   - running → ⏳ 翻譯中 (filename)
//   - done    → ✅ 翻譯完成 (點擊下載)
//   - failed  → ❌ 翻譯失敗
//   - idle    → hidden

(function () {
    const POLL_INTERVAL_MS = 3000;       // 3 秒 poll
    const DONE_HIDE_AFTER_MS = 30000;    // 完成後 30 秒隱藏

    const badge = document.getElementById("jpNavbarStatus");
    const label = document.getElementById("jpNavbarBadge");
    if (!badge || !label) return;  // user 不是 admin/家人,沒 indicator

    let doneShownAt = null;  // 完成時間,計算隱藏
    let lastState = null;

    async function poll() {
        try {
            const r = await fetch("/api/tools/jp-status");
            if (!r.ok) return;  // 403 表示 user 沒權限 (理論上不會,因為 template 已 gate)
            const s = await r.json();
            update(s);
        } catch (e) {
            // 網路錯誤 — 維持上一個狀態
        }
    }

    function update(s) {
        const state = s.state;

        // 完成後 30 秒自動隱藏
        if (state === "done") {
            if (doneShownAt === null) doneShownAt = Date.now();
            if (Date.now() - doneShownAt > DONE_HIDE_AFTER_MS) {
                badge.hidden = true;
                return;
            }
        } else {
            doneShownAt = null;
        }

        if (state === "idle") {
            badge.hidden = true;
            lastState = null;
            return;
        }

        // 顯示 indicator
        const filename = s.filename || "(unknown)";
        const engine = s.engine || "?";
        let icon, color, title;

        if (state === "queued") {
            icon = "⏳";
            color = "secondary";
            title = `排隊中: ${filename} (${engine})`;
        } else if (state === "running") {
            icon = "⏳";
            color = "warning";
            title = `翻譯中: ${filename} (${engine})`;
        } else if (state === "done") {
            icon = "✅";
            color = "success";
            title = `翻譯完成: ${filename} (${engine}) — 點擊下載`;
        } else if (state === "failed") {
            icon = "❌";
            color = "danger";
            title = `翻譯失敗: ${filename} (${engine}) — 點擊查看`;
        } else {
            return;  // 未知 state
        }

        // 短 filename 顯示 (避免太長破壞 navbar)
        const shortName = filename.length > 18
            ? filename.slice(0, 8) + "..." + filename.slice(-7)
            : filename;

        label.textContent = `${icon} ${state === "done" || state === "failed" ? "翻譯" + (state === "done" ? "完成" : "失敗") : "翻譯中"}`;

        // Set badge class (color)
        badge.className = `navbar-badge d-inline-flex align-items-center badge bg-${color}`;
        badge.title = title;

        // 重新啟用 tooltip (Bootstrap 5 需要 dispose + new)
        if (window.bootstrap && bootstrap.Tooltip) {
            const existing = bootstrap.Tooltip.getInstance(badge);
            if (existing) existing.dispose();
            new bootstrap.Tooltip(badge);
        }

        badge.hidden = false;
        lastState = state;
    }

    // Start polling
    poll();
    setInterval(poll, POLL_INTERVAL_MS);
})();