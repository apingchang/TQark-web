
// === 【2026-08-01 改】日文翻譯 UI: file input + upload + download ===
const jpState = {
    polling: null,
    currentFile: null,
    currentTaskId: null,
};

function getEngine() {
    const el = document.querySelector('input[name="jpEngine"]:checked');
    return el ? el.value : "all";
}

function setJpUIState(state) {
    const badge = document.getElementById("jpStateBadge");
    const startBtn = document.getElementById("jpStartBtn");
    const fileInput = document.getElementById("jpFileInput");
    const engineInputs = document.querySelectorAll('input[name="jpEngine"]');
    
    if (state === "idle") {
        badge.className = "badge bg-secondary";
        badge.textContent = "閒置";
        startBtn.disabled = !jpState.currentFile;
    } else if (state === "queued" || state === "running") {
        badge.className = "badge bg-warning text-dark";
        badge.textContent = state === "running" ? "⏳ 翻譯中..." : "排隊中";
        startBtn.disabled = true;
    } else if (state === "done") {
        badge.className = "badge bg-success";
        badge.textContent = "✅ 完成";
        startBtn.disabled = !jpState.currentFile;
    } else if (state === "failed") {
        badge.className = "badge bg-danger";
        badge.textContent = "❌ 失敗";
        startBtn.disabled = !jpState.currentFile;
    }
}

async function jpStart() {
    if (!jpState.currentFile) {
        alert("請先選擇 .docx 檔案");
        return;
    }
    
    const radio = document.querySelector("input[name=jpEngine]:checked"); const engine = radio ? radio.value : "all";
    const startBtn = document.getElementById("jpStartBtn");
    startBtn.disabled = true;
    
    try {
        // 上傳檔案 + 啟動翻譯 (multipart/form-data)
        const formData = new FormData();
        formData.append("file", jpState.currentFile);
        formData.append("engine", engine);
        
        const r = await fetch("/api/tools/jp-upload", {
            method: "POST",
            body: formData,
        });
        if (!r.ok) {
            const err = await r.json();
            throw new Error(err.detail || err.error || `HTTP ${r.status}`);
        }
        const data = await r.json();
        if (!data.ok) throw new Error(data.error || "啟動失敗");
        
        jpState.currentTaskId = data.task_id;
        setJpUIState("running");
        renderStatus({state: "running", filename: data.filename, engine: data.engine, stdout: [], stderr: []});
        startPolling();
    } catch (e) {
        alert(`啟動失敗: ${e.message}`);
        setJpUIState("idle");
        console.error(e);
    }
}

function renderStatus(status) {
    const area = document.getElementById("jpStatusArea");
    if (!status || status.state === "idle") {
        area.innerHTML = '<div class="alert alert-secondary py-2 px-3 small mb-0"><em>📋 請選擇 .docx 檔案 + 引擎,然後按「開始翻譯」。</em></div>';
        return;
    }
    
    const stateText = {
        queued: "⏳ 排隊中",
        running: "⏳ 翻譯中...",
        done: "✅ 完成",
        failed: "❌ 失敗",
    }[status.state] || status.state;
    
    const engineText = {
        google: "Google",
        minimax: "MiniMax",
        all: "Google + MiniMax",
    }[status.engine] || status.engine;
    
    let html = `<div class="alert alert-${status.state === 'done' ? 'success' : status.state === 'failed' ? 'danger' : 'warning'} py-2 px-3 small mb-2">`;
    html += `<strong>${stateText}</strong> — ${status.filename} (${engineText})`;
    if (status.started_at) html += `<br><small>開始: ${status.started_at}</small>`;
    if (status.finished_at) html += `<br><small>完成: ${status.finished_at}</small>`;
    if (status.exit_code !== null) html += `<br><small>Exit code: ${status.exit_code}</small>`;
    html += `</div>`;
    
    // Download buttons
    if (status.state === "done" && status.output_files && status.output_files.length > 0) {
        html += `<div class="alert alert-info py-2 px-3 small mb-2"><strong>📤 下載翻譯結果:</strong><div class="mt-2 d-flex gap-2 flex-wrap">`;
        status.output_files.forEach(f => {
            const fname = f.split('/').pop();
            const engine = fname.endsWith('_google.docx') ? 'google' : 
                          fname.endsWith('_minimax.docx') ? 'minimax' : null;
            if (engine && status.task_id) {
                html += `<a href="/api/tools/jp-download/${status.task_id}/${engine}" 
                           class="btn btn-sm btn-success" download>
                           📥 ${engine} (${fname})
                         </a>`;
            } else {
                html += `<code>${fname}</code>`;
            }
        });
        html += `</div></div>`;
    }
    
    if (status.stdout && status.stdout.length > 0) {
        html += `<details class="small mb-1"><summary>📋 stdout (${status.stdout.length} 行)</summary><pre class="bg-light p-2 small mt-1" style="max-height: 200px; overflow-y: auto;">`;
        html += status.stdout.slice(-50).map(l => escapeHtml(l)).join("\n");
        html += `</pre></details>`;
    }
    
    if (status.stderr && status.stderr.length > 0) {
        html += `<details class="small mb-1"><summary>⚠️ stderr (${status.stderr.length} 行)</summary><pre class="bg-light p-2 small mt-1" style="max-height: 200px; overflow-y: auto;">`;
        html += status.stderr.slice(-50).map(l => escapeHtml(l)).join("\n");
        html += `</pre></details>`;
    }
    
    area.innerHTML = html;
}

function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[c]));
}

async function pollStatus() {
    try {
        const r = await fetch("/api/tools/jp-status");
        if (!r.ok) return;
        const status = await r.json();
        setJpUIState(status.state);
        renderStatus(status);
        
        if (status.state === "done" || status.state === "failed" || status.state === "idle") {
            stopPolling();
        }
    } catch (e) {
        console.error("poll failed:", e);
    }
}

function startPolling() {
    if (jpState.polling) return;
    jpState.polling = setInterval(pollStatus, 1500);
}

function stopPolling() {
    if (jpState.polling) {
        clearInterval(jpState.polling);
        jpState.polling = null;
    }
}

// === Event listeners ===
document.addEventListener("DOMContentLoaded", () => {
    const fileInput = document.getElementById("jpFileInput");
    const selectedFileDiv = document.getElementById("jpSelectedFile");
    const startBtn = document.getElementById("jpStartBtn");
    
    fileInput.addEventListener("change", (e) => {
        const file = e.target.files[0];
        if (file) {
            jpState.currentFile = file;
            selectedFileDiv.textContent = `✓ 已選: ${file.name} (${(file.size/1024).toFixed(1)} KB)`;
            startBtn.disabled = false;
        } else {
            jpState.currentFile = null;
            selectedFileDiv.textContent = "";
            startBtn.disabled = true;
        }
    });
    
    startBtn.addEventListener("click", jpStart);
    
    // Initial status check
    pollStatus();
});

