// === Sidebar 互動 helpers (CSP 不准 inline) ===

document.addEventListener("DOMContentLoaded", () => {
    // 權限 toggle icon rotation
    const permCollapse = document.getElementById("permissionDetails");
    const permIcon = document.getElementById("permissionToggleIcon");
    if (permCollapse && permIcon) {
        permCollapse.addEventListener("show.bs.collapse", () => {
            permIcon.textContent = "▾";
        });
        permCollapse.addEventListener("hide.bs.collapse", () => {
            permIcon.textContent = "▸";
        });
    }

    // 【2026-08-03 新】考題資訊 card — 各縣市 / 最新檔案折收 toggle arrow
    // 同步時所有 .toggle-collapsed 都會被註冊 show/hide listener
    document.querySelectorAll(".toggle-collapsed").forEach((toggle) => {
        const targetSel = toggle.getAttribute("href");
        if (!targetSel) return;
        const target = document.querySelector(targetSel);
        const arrow = toggle.querySelector(".collapse-arrow");
        if (!target || !arrow) return;
        target.addEventListener("show.bs.collapse", () => {
            arrow.textContent = "▾";
            toggle.setAttribute("aria-expanded", "true");
        });
        target.addEventListener("hide.bs.collapse", () => {
            arrow.textContent = "▸";
            toggle.setAttribute("aria-expanded", "false");
        });
    });

    // 【2026-08-07 移除】「返回上一層」改成純 <a href="...">, 不再用 history.back()
});
