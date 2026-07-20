// =========================================================
// 我的下載歷史 — 刪除功能
// =========================================================
let pendingDelete = null;  // {recordId, filename}

// 偵測 File System Access API 支援
const HAS_FSA = ('showDirectoryPicker' in window);

document.addEventListener('DOMContentLoaded', () => {
    // 顯示 / 隱藏「不支援」提示
    if (!HAS_FSA) {
        const el = document.getElementById('deleteModalFsaUnsupported');
        if (el) el.classList.remove('d-none');
    }

    // 每個 刪除 按鈕 click
    document.querySelectorAll('.delete-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            pendingDelete = {
                recordId: btn.dataset.recordId,
                filename: btn.dataset.filename,
            };
            document.getElementById('deleteModalFilename').textContent = btn.dataset.filename;
            const modal = new bootstrap.Modal(document.getElementById('deleteModal'));
            modal.show();
        });
    });

    // 確認刪除
    document.getElementById('deleteModalConfirm').addEventListener('click', async () => {
        if (!pendingDelete) return;

        const alsoLocal = document.getElementById('deleteModalLocal').checked;
        const modalEl = document.getElementById('deleteModal');

        // 關 modal
        bootstrap.Modal.getInstance(modalEl).hide();

        // 先記下 row(失敗時要 restore)
        const row = document.querySelector(`button.delete-btn[data-record-id="${pendingDelete.recordId}"]`)?.closest('tr');
        const rowHtml = row?.outerHTML;

        // 立刻從 DOM 拿掉(讓 user 感覺很快)
        if (row) row.style.opacity = '0.3';

        try {
            // 1) 刪本機檔案 (如果勾選 + 支援 FSA)
            if (alsoLocal && HAS_FSA) {
                try {
                    const removed = await tryDeleteLocalFile(pendingDelete.filename);
                    if (!removed) {
                        // 使用者取消資料夾選擇 或 找不到檔案 → 繼續刪 server 紀錄(讓 user 不要卡住)
                        // 不報錯,讓 user 知道狀況就好
                    }
                } catch (fsErr) {
                    console.warn('FSA delete failed:', fsErr);
                    // 繼續刪 server 紀錄
                }
            } else if (alsoLocal && !HAS_FSA) {
                // 不支援 FSA → 提示手動刪
                alert(`本機檔案刪除需要 Chrome 或 Edge 瀏覽器。\n\n請手動刪除:\n${pendingDelete.filename}`);
            }

            // 2) 刪 server 紀錄
            const resp = await fetch(`/me/downloads/${pendingDelete.recordId}/delete`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
            });

            if (!resp.ok) {
                const errText = await resp.text();
                alert(`刪除失敗: ${resp.status} ${errText.slice(0, 200)}`);
                // restore row
                if (row && rowHtml) row.outerHTML = rowHtml;
                return;
            }

            // 3) 從 DOM 移除(淡出動畫)
            if (row) {
                row.style.transition = 'opacity 0.4s';
                setTimeout(() => row.remove(), 400);
            }

            // 更新總數
            const badge = document.querySelector('.badge.bg-secondary.fs-6');
            if (badge) {
                const m = badge.textContent.match(/(\d+)/);
                if (m) {
                    const newCount = Math.max(0, parseInt(m[1]) - 1);
                    badge.textContent = `${newCount} 筆紀錄`;
                }
            }

        } catch (err) {
            alert(`錯誤: ${err}`);
            if (row && rowHtml) row.outerHTML = rowHtml;
        }

        pendingDelete = null;
    });
});


// =========================================================
// 用 File System Access API 試刪本機檔案
// =========================================================
async function tryDeleteLocalFile(targetFilename) {
    // 1) user 選資料夾
    let dirHandle;
    try {
        dirHandle = await window.showDirectoryPicker({mode: 'readwrite'});
    } catch (e) {
        // 使用者取消
        if (e.name === 'AbortError') {
            alert('已取消資料夾選擇。\n本機檔案不會被刪除,但 TQark-web 上的紀錄仍會刪除。');
            return false;
        }
        throw e;
    }

    // 2) 在資料夾內找對應檔名(只找一層,不去 recursive)
    let fileHandle = null;
    for await (const [name, handle] of dirHandle.entries()) {
        if (handle.kind === 'file' && name === targetFilename) {
            fileHandle = handle;
            break;
        }
    }

    if (!fileHandle) {
        alert(`在選定的資料夾內找不到檔案:\n${targetFilename}\n\n請確認你選的是下載資料夾,或手動刪除。`);
        return false;
    }

    // 3) 確認 (安全機制)
    const ok = confirm(`找到檔案:\n${targetFilename}\n\n確定刪除嗎?此操作無法復原。`);
    if (!ok) return false;

    // 4) 刪
    await dirHandle.removeEntry(targetFilename);
    alert(`✅ 已刪除: ${targetFilename}`);
    return true;
}

// =========================================================
// 全部刪除 (2026-07-20 新增)
// =========================================================
const deleteAllBtn = document.getElementById('deleteAllBtn');
const deleteAllModal = document.getElementById('deleteAllModal');

if (deleteAllBtn && deleteAllModal) {
    // 從當前 URL 解析篩選條件 + 從後端抓總數
    const url = new URL(window.location.href);
    const filterParams = new URLSearchParams();
    if (url.searchParams.get('school_year')) filterParams.set('school_year', url.searchParams.get('school_year'));
    if (url.searchParams.get('subject')) filterParams.set('subject', url.searchParams.get('subject'));
    if (url.searchParams.get('filetype')) filterParams.set('filetype', url.searchParams.get('filetype'));

    function buildDeleteAllQS() {
        const params = new URLSearchParams();
        if (url.searchParams.get('school_year')) params.set('school_year', url.searchParams.get('school_year'));
        if (url.searchParams.get('subject')) params.set('subject', url.searchParams.get('subject'));
        if (url.searchParams.get('filetype')) params.set('filetype', url.searchParams.get('filetype'));
        return params.toString();
    }

    deleteAllBtn.addEventListener('click', async () => {
        // 從後端拿實際符合條件的 record 數(不分頁)
        // 用 HEAD trick 不行,我們用 GET 拿 total
        const resp = await fetch('/me/downloads?' + buildDeleteAllQS() + '&page=1', {
            headers: {'Accept': 'text/html'}
        });
        const html = await resp.text();
        // 解析 'N 筆紀錄' (badge) 或 fallback '共 N 筆'
        let m = html.match(/(\d+)\s*筆紀錄/);
        if (!m) m = html.match(/共\s*<strong>(\d+)<\/strong>\s*筆/);
        const total = m ? parseInt(m[1]) : 0;

        if (total === 0) {
            alert('目前沒有可刪除的下載紀錄。');
            return;
        }

        // 填 modal
        document.getElementById('deleteAllTotalCount').textContent = total;

        // 篩選條件說明
        const filters = [];
        if (url.searchParams.get('school_year')) filters.push(`學年:${url.searchParams.get('school_year')}`);
        if (url.searchParams.get('subject')) filters.push(`科目:${url.searchParams.get('subject')}`);
        if (url.searchParams.get('filetype')) {
            filters.push(`類型:${url.searchParams.get('filetype') === 'paper' ? '試卷' : '答案'}`);
        }
        document.getElementById('deleteAllFilterNote').textContent =
            filters.length > 0
                ? `套用篩選:${filters.join('、')}`
                : '⚠️ 沒有套用篩選 → 會刪除你的全部紀錄';

        // 顯示 modal
        const modal = new bootstrap.Modal(deleteAllModal);
        modal.show();
    });

    document.getElementById('deleteAllConfirm').addEventListener('click', async () => {
        const btn = document.getElementById('deleteAllConfirm');
        btn.disabled = true;
        btn.textContent = '刪除中...';

        const deleteLocal = document.getElementById('deleteAllLocal').checked;

        try {
            // 1) 拿所有將被刪除的 record 資料(用來後面刪本機)
            const listResp = await fetch('/me/downloads?' + buildDeleteAllQS() + '&page=1');
            const listHtml = await listResp.text();
            // 從 HTML 抽出 download_filename 列表
            const filenames = [];
            const re = /data-filename="([^"]+)"/g;
            let mm;
            while ((mm = re.exec(listHtml)) !== null) {
                filenames.push(decodeURIComponent(mm[1]).replace(/&amp;/g, '&'));
            }
            // 因為分頁只顯示 25 筆,實際上如果 total > 25 需要處理
            // 解法:分批抓所有 page 拿完整 list
            const totalMatch = listHtml.match(/(\d+)\s*筆紀錄/) || listHtml.match(/共\s*<strong>(\d+)<\/strong>\s*筆/);
            const total = totalMatch ? parseInt(totalMatch[1]) : 0;
            if (total > 25) {
                // 抓所有 page
                for (let p = 2; p <= Math.ceil(total / 25); p++) {
                    const r = await fetch('/me/downloads?' + buildDeleteAllQS() + '&page=' + p);
                    const h = await r.text();
                    const re2 = /data-filename="([^"]+)"/g;
                    while ((mm = re2.exec(h)) !== null) {
                        filenames.push(decodeURIComponent(mm[1]).replace(/&amp;/g, '&'));
                    }
                }
            }

            // 2) 打 API 刪除所有 DB records
            const resp = await fetch('/me/downloads/delete-all?' + buildDeleteAllQS(), {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({}),
            });
            if (!resp.ok) {
                const err = await resp.text();
                alert('刪除失敗:' + err);
                return;
            }
            const data = await resp.json();

            // 3) 關 modal
            bootstrap.Modal.getInstance(deleteAllModal).hide();

            // 4) (可選) 刪本機檔案
            let deletedLocal = 0;
            let localSkipped = 0;
            if (deleteLocal && filenames.length > 0 && window.showDirectoryPicker) {
                try {
                    const dirHandle = await window.showDirectoryPicker({mode: 'readwrite'});
                    for (const fname of filenames) {
                        try {
                            // 直接嘗試 remove,不先列舉(更快)
                            await dirHandle.removeEntry(fname);
                            deletedLocal++;
                        } catch (err) {
                            localSkipped++;
                        }
                    }
                } catch (err) {
                    console.warn('showDirectoryPicker 取消或失敗', err);
                }
            }

            // 5) 顯示結果 + 重新整理
            let msg = `✅ 已刪除 ${data.deleted_count} 筆下載紀錄`;
            if (deleteLocal) {
                if (deletedLocal > 0) msg += `\n📁 本機檔案:成功 ${deletedLocal} 個`;
                if (localSkipped > 0) msg += `、沒找到 ${localSkipped} 個`;
            }
            alert(msg);
            // 重新整理(回到 page 1)
            window.location.href = '/me/downloads';
        } catch (err) {
            alert('錯誤:' + err);
        } finally {
            btn.disabled = false;
            btn.textContent = '確認刪除';
        }
    });
}
