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

    // 返回上一頁 handler
    document.querySelectorAll(".js-back-link").forEach(link => {
        link.addEventListener("click", (e) => {
            e.preventDefault();
            // 如果有 history, 回上一頁; 否則回首頁
            if (window.history.length > 1) {
                window.history.back();
            } else {
                window.location.href = "/";
            }
        });
    });
});
