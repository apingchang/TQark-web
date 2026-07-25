// =========================================================
// CAP/CEEC archive batch 下載 client-side 邏輯
// 跟 search_results.js 的 StudyArk batch UX 一樣, 但:
// - 來源: 本地 archive (CAP/CEEC), 不用抓 StudyArk
// - 不限流, 沒 10 秒/item 等待
// - key 用 `${source}_${rel}` (因為 archive item shape 不同)
// =========================================================
const MAX_BATCH = 20;
const STORAGE_KEY = 'tqark_archive_selected_items';

function getSelected() {
    try {
        return JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
    } catch { return {}; }
}

function setSelected(items) {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(items));
}

function makeKey(d) {
    return `${d.source}_${d.rel}`;
}

function updateBatchBar() {
    const selected = getSelected();
    const count = Object.keys(selected).length;
    // 頂部 bar
    const bar = document.getElementById('batchBar');
    const counter = document.getElementById('selectedCount');
    if (counter) counter.textContent = count;
    if (bar) {
        if (count > 0) bar.classList.remove('d-none');
        else bar.classList.add('d-none');
    }
    // 底部 bar
    const counterBottom = document.getElementById('selectedCountBottom');
    if (counterBottom) counterBottom.textContent = count;
    const dlBtnBottom = document.getElementById('downloadBatchBtnBottom');
    const clearBtnBottom = document.getElementById('clearSelectionBtnBottom');
    if (dlBtnBottom) dlBtnBottom.disabled = count === 0;
    if (clearBtnBottom) clearBtnBottom.disabled = count === 0;

    // 標記已勾選的 checkbox (跨 page 保留)
    const allItemChks = document.querySelectorAll('.itemChk');
    allItemChks.forEach(chk => {
        const k = makeKey(chk.dataset);
        if (selected[k]) {
            chk.checked = true;
            const row = chk.closest('tr');
            if (row) row.style.backgroundColor = '#fff3cd';
        } else {
            chk.checked = false;
            const row = chk.closest('tr');
            if (row) row.style.backgroundColor = '';
        }
    });

    // 同步 selectAllChk (本頁全勾才打勾)
    const selectAll = document.getElementById('selectAllChk');
    if (selectAll && allItemChks.length > 0) {
        const allChecked = Array.from(allItemChks).every(c => c.checked);
        selectAll.checked = allChecked;
    }
}

// 勾選變動時
document.addEventListener('change', e => {
    if (!e.target.classList.contains('itemChk')) return;
    const selected = getSelected();
    const k = makeKey(e.target.dataset);
    if (e.target.checked) {
        if (Object.keys(selected).length >= MAX_BATCH) {
            e.target.checked = false;
            alert(`單批最多 ${MAX_BATCH} 個!請先取消其他勾選後再勾。`);
            return;
        }
        selected[k] = {
            source: e.target.dataset.source,
            rel: e.target.dataset.rel,
            title: e.target.dataset.title,
            subject: e.target.dataset.subject,
            grade: e.target.dataset.grade,
            school_year: e.target.dataset.schoolYear,
        };
    } else {
        delete selected[k];
    }
    setSelected(selected);
    updateBatchBar();
});

// 全選
document.getElementById('selectAllChk').addEventListener('change', e => {
    const want = e.target.checked;
    const itemChks = document.querySelectorAll('.itemChk');
    let selected = getSelected();
    let currentCount = Object.keys(selected).length;

    itemChks.forEach(chk => {
        const k = makeKey(chk.dataset);
        if (want) {
            if (!selected[k] && currentCount < MAX_BATCH) {
                selected[k] = {
                    source: chk.dataset.source,
                    rel: chk.dataset.rel,
                    title: chk.dataset.title,
                    subject: chk.dataset.subject,
                    grade: chk.dataset.grade,
                    school_year: chk.dataset.schoolYear,
                };
                chk.checked = true;
                currentCount++;
            } else if (selected[k]) {
                chk.checked = true;
            } else {
                chk.checked = false;
            }
        } else {
            if (selected[k]) delete selected[k];
            chk.checked = false;
        }
    });
    setSelected(selected);
    updateBatchBar();
});

// 清除選取 (頂部)
document.getElementById('clearSelectionBtn').addEventListener('click', () => {
    setSelected({});
    updateBatchBar();
});
// 清除選取 (底部)
document.getElementById('clearSelectionBtnBottom').addEventListener('click', () => {
    setSelected({});
    updateBatchBar();
});

// 下載按鈕 → 確認 modal
function openConfirmModal() {
    const selected = getSelected();
    const items = Object.values(selected);
    if (items.length === 0) return;
    if (items.length > MAX_BATCH) {
        alert(`單批最多 ${MAX_BATCH} 個,你選了 ${items.length} 個。\n請取消勾選部分後再下載。`);
        return;
    }
    document.getElementById('confirmCount').textContent = items.length;
    const ol = document.getElementById('confirmList');
    ol.innerHTML = '';
    items.forEach((it) => {
        const li = document.createElement('li');
        const sourceLabel = it.source === 'CAP' ? '會考' : '大考';
        li.textContent = `[${sourceLabel}] ${it.title || it.rel}`;
        ol.appendChild(li);
    });
    document.getElementById('confirmError').classList.add('d-none');
    const modal = new bootstrap.Modal(document.getElementById('confirmModal'));
    modal.show();
}

document.getElementById('downloadBatchBtn').addEventListener('click', openConfirmModal);
document.getElementById('downloadBatchBtnBottom').addEventListener('click', () => {
    const selected = getSelected();
    if (Object.keys(selected).length === 0) {
        alert('請先勾選考古題!');
        return;
    }
    openConfirmModal();
});

// 確認下載 → 打 API
document.getElementById('confirmDownloadBtn').addEventListener('click', async () => {
    const selected = getSelected();
    const items = Object.values(selected);
    if (items.length === 0) return;

    bootstrap.Modal.getInstance(document.getElementById('confirmModal')).hide();
    const progressBar = document.getElementById('progressBar');
    const progressText = document.getElementById('progressText');
    progressBar.style.width = '30%';
    progressBar.textContent = '30%';
    progressBar.classList.add('progress-bar-animated');
    progressBar.classList.remove('bg-success', 'bg-warning', 'bg-danger');
    progressText.textContent = `打包 ${items.length} 個本地 archive PDF...`;
    const progressModal = new bootstrap.Modal(document.getElementById('progressModal'));
    progressModal.show();

    try {
        const resp = await fetch('/api/batch-download-archive', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({items: items}),
        });

        if (!resp.ok) {
            let errText = await resp.text();
            const friendlyMsg = `下載失敗 (HTTP ${resp.status})`;
            progressBar.classList.add('bg-danger');
            progressBar.classList.remove('progress-bar-animated');
            progressText.innerHTML = `<span class="text-warning">${friendlyMsg}<br><small class="text-muted">${errText.slice(0, 200)}</small></span>`;
            setTimeout(() => progressModal.hide(), 8000);
            return;
        }

        progressBar.style.width = '90%';
        progressBar.textContent = '90%';
        progressText.textContent = '打包完成,瀏覽器正在儲存...';

        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        const disposition = resp.headers.get('Content-Disposition');
        let zipName = 'tqark_archive.zip';
        if (disposition) {
            const m = disposition.match(/filename="([^"]+)"/);
            if (m) zipName = m[1];
        }
        a.download = zipName;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);

        // 從選取清單移除已下載
        items.forEach(it => {
            const k = makeKey(it);
            const sel = getSelected();
            delete sel[k];
            setSelected(sel);
        });
        updateBatchBar();

        // 標記 row 為「已下載」
        items.forEach(it => {
            document.querySelectorAll('.itemChk').forEach(chk => {
                if (makeKey(chk.dataset) === makeKey(it)) {
                    chk.disabled = true;
                    const row = chk.closest('tr');
                    if (row) {
                        row.style.backgroundColor = '#d1e7dd';
                        const statusCell = row.querySelector('td:last-child');
                        if (statusCell) statusCell.innerHTML = '<span class="text-success">✓ 已下載</span>';
                    }
                }
            });
        });

        const downloaded = resp.headers.get('X-Downloaded-Count') || items.length;
        const errors = resp.headers.get('X-Error-Count') || 0;
        progressBar.style.width = '100%';
        progressBar.textContent = '100%';
        progressBar.classList.remove('progress-bar-animated');
        progressBar.classList.add('bg-success');
        progressText.textContent = `✅ 下載完成: ${zipName} (${downloaded} 個檔案${errors > 0 ? `, ${errors} 個失敗` : ''})`;
        setTimeout(() => progressModal.hide(), 2000);

    } catch (err) {
        progressBar.classList.add('bg-danger');
        progressBar.classList.remove('progress-bar-animated');
        progressText.innerHTML = `<span class="text-danger">網路錯誤: ${err.message}</span>`;
        setTimeout(() => progressModal.hide(), 8000);
    }
});

// 初始更新 (跨頁保留勾選)
updateBatchBar();