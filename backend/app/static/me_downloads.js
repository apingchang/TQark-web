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