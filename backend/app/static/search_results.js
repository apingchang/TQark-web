// =========================================================
// 勾選下載的 client-side 邏輯
// =========================================================
const MAX_BATCH = 20;
const STORAGE_KEY = 'tqark_selected_items';

function getSelected() {
    try {
        return JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
    } catch { return {}; }
}

function setSelected(items) {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(items));
}

function makeKey(it) {
    return `${it.classid}_${it.fileid}_${it.filetype}`;
}

function updateBatchBar() {
    const selected = getSelected();
    const count = Object.keys(selected).length;
    // 頂部 bar
    const bar = document.getElementById('batchBar');
    const counter = document.getElementById('selectedCount');
    if (counter) counter.textContent = count;
    if (bar) {
        if (count > 0) {
            bar.classList.remove('d-none');
        } else {
            bar.classList.add('d-none');
        }
    }
    // 底部 bar(始終顯示,只是按钮根據 count 來 enable/disable)
    const counterBottom = document.getElementById('selectedCountBottom');
    if (counterBottom) counterBottom.textContent = count;
    const dlBtnBottom = document.getElementById('downloadBatchBtnBottom');
    const clearBtnBottom = document.getElementById('clearSelectionBtnBottom');
    if (dlBtnBottom) dlBtnBottom.disabled = count === 0;
    if (clearBtnBottom) clearBtnBottom.disabled = count === 0;

    // 標記已勾選的 checkbox(跨 page 保留)
    const allItemChks = document.querySelectorAll('.itemChk');
    allItemChks.forEach(chk => {
        const k = makeKey(chk.dataset);
        if (selected[k]) {
            chk.checked = true;
            // 標記 row 狀態
            const row = chk.closest('tr');
            if (row) {
                row.style.backgroundColor = '#fff3cd';
            }
        } else {
            chk.checked = false;
            const row = chk.closest('tr');
            if (row) row.style.backgroundColor = '';
        }
    });

    // 同步 selectAllChk(header 全選 checkbox):
    // 只有「本頁所有的 itemChk 都被勾」才打勾、否則取消
    // (跨頁勾選的項目 不會讓 selectAllChk 變 checked,因為 header 全選只能全選當頁)
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
        // 上限 20 個
        if (Object.keys(selected).length >= MAX_BATCH) {
            e.target.checked = false;
            alert(`單批最多 ${MAX_BATCH} 個!請先取消其他勾選後再勾。`);
            return;
        }
        selected[k] = {
            classid: e.target.dataset.classid,
            fileid: e.target.dataset.fileid,
            filetype: e.target.dataset.filetype,
            title: e.target.dataset.title,
            school_name: e.target.dataset.school,
            grade: e.target.dataset.grade,
            school_year: e.target.dataset.year,
            school_term: e.target.dataset.term,
            category: e.target.dataset.cat,
            subject: e.target.dataset.subject,
            exam_type: e.target.dataset.type,
            version: e.target.dataset.version,
        };
    } else {
        delete selected[k];
    }
    setSelected(selected);
    updateBatchBar();
});

// 全選
// 直接改 localStorage、不 dispatch change event
// (原本用 dispatchEvent cascade, 但 updateBatchBar() 會在 forEach 中被多次
// 觸發、每次都重設 selectAll.checked = false, 造成只剩第一個勾選)
document.getElementById('selectAllChk').addEventListener('change', e => {
    const want = e.target.checked;
    const itemChks = document.querySelectorAll('.itemChk');
    let selected = getSelected();

    // 計算「要勾幾個 / 現在勾幾個」
    const currentCount = Object.keys(selected).length;

    itemChks.forEach(chk => {
        const k = makeKey(chk.dataset);
        if (want) {
            // 勾: 不能超過 MAX_BATCH
            if (!selected[k] && currentCount < MAX_BATCH) {
                selected[k] = {
                    classid: chk.dataset.classid,
                    fileid: chk.dataset.fileid,
                    filetype: chk.dataset.filetype,
                    title: chk.dataset.title,
                    school_name: chk.dataset.school,
                    grade: chk.dataset.grade,
                    school_year: chk.dataset.year,
                    school_term: chk.dataset.term,
                    category: chk.dataset.cat,
                    subject: chk.dataset.subject,
                    exam_type: chk.dataset.type,
                    version: chk.dataset.version,
                };
                chk.checked = true;
                currentCount++;
            } else if (selected[k]) {
                // 已經勾了
                chk.checked = true;
            } else {
                // 會超過 20、不勾
                chk.checked = false;
            }
        } else {
            // 取消勾選
            if (selected[k]) delete selected[k];
            chk.checked = false;
        }
    });
    setSelected(selected);
    updateBatchBar();
});

// 清除選取(頂部)
document.getElementById('clearSelectionBtn').addEventListener('click', () => {
    setSelected({});
    updateBatchBar();
});
// 清除選取(底部)
document.getElementById('clearSelectionBtnBottom').addEventListener('click', () => {
    setSelected({});
    updateBatchBar();
});

// 「下載勾選項目」按鈕 → 顯示 confirm modal(頂部)
document.getElementById('downloadBatchBtn').addEventListener('click', () => {
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
    items.forEach((it, i) => {
        const li = document.createElement('li');
        li.textContent = `[${it.filetype === 'daan' ? '答案' : '試卷'}] ${it.title || it.school_name}`;
        ol.appendChild(li);
    });
    document.getElementById('confirmError').classList.add('d-none');
    const modal = new bootstrap.Modal(document.getElementById('confirmModal'));
    modal.show();
});

// 「下載勾選項目」按鈕(底部) → 同上
document.getElementById('downloadBatchBtnBottom').addEventListener('click', () => {
    const selected = getSelected();
    if (Object.keys(selected).length === 0) {
        alert('請先勾選考古題!');
        return;
    }
    document.getElementById('downloadBatchBtn').click();
});

// 「確認下載」→ 實際打 API
document.getElementById('confirmDownloadBtn').addEventListener('click', async () => {
    const selected = getSelected();
    const items = Object.values(selected);
    if (items.length === 0) return;

    // 關 confirm, 顯示 progress
    bootstrap.Modal.getInstance(document.getElementById('confirmModal')).hide();
    const progressBar = document.getElementById('progressBar');
    const progressText = document.getElementById('progressText');
    progressBar.style.width = '30%';
    progressBar.textContent = '30%';
    progressText.textContent = `正在向 StudyArk 抓 ${items.length} 個檔案...`;
    const progressModal = new bootstrap.Modal(document.getElementById('progressModal'));
    progressModal.show();

    try {
        const resp = await fetch('/api/batch-download', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({items: items}),
        });

        if (!resp.ok) {
            let errText = await resp.text();
            let friendlyMsg = '';
            if (resp.status === 429) {
                // StudyArk 限流 → 答合友善訊息
                friendlyMsg = 'StudyArk 限流中(下載太頻繁、啟動 anti-bot)。請等 25 分鐘後再試。';
                progressBar.classList.add('bg-warning');
            } else {
                friendlyMsg = `下載失敗 (HTTP ${resp.status})`;
                progressBar.classList.add('bg-danger');
            }
            progressBar.classList.remove('progress-bar-animated');
            progressText.innerHTML = `<span class="text-warning">${friendlyMsg}<br><small class="text-muted">${errText.slice(0, 200)}</small></span>`;
            return;
        }

        progressBar.style.width = '90%';
        progressBar.textContent = '90%';
        progressText.textContent = '打包完成,瀏覽器正在儲存...';

        // 觸發瀏覽器下載
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        const disposition = resp.headers.get('Content-Disposition');
        let zipName = 'tqark_exams.zip';
        if (disposition) {
            const m = disposition.match(/filename="([^"]+)"/);
            if (m) zipName = m[1];
        }
        a.download = zipName;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);

        // 從選取清單中移除已下載的
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

        progressBar.style.width = '100%';
        progressBar.textContent = '100%';
        progressBar.classList.remove('progress-bar-animated');
        progressText.textContent = `✅ 下載完成: ${zipName}`;
        setTimeout(() => progressModal.hide(), 2000);

    } catch (err) {
        progressBar.classList.remove('progress-bar-animated');
        progressBar.classList.add('bg-danger');
        progressText.innerHTML = `<span class="text-danger">錯誤:${err}</span>`;
    }
});

// 初始化
updateBatchBar();
