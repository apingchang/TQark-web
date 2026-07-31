"""
【2026-07-31 新】日文翻譯 task runner
- 從 TQark-web /tools/jp-translate page 觸發
- 背景 process 跑 translate_engines_v2.py
- Status 即時更新 (前端 polling)

Workflow:
1. User 選 inbox folder + 選 file
2. User 選 outbox folder (預設 done)
3. User 選 engine (google / minimax / all)
4. Click 開始翻譯
5. Backend 跑 translate.sh (or v2 直接呼叫) in background
6. Status 更新: queued → running → done / failed
"""
import os
import json
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# === 翻譯 script 路徑 ===
TRANSLATION_DIR = Path("/home/aping/.openclaw/workspace/translation")
TRANSLATE_SCRIPT = TRANSLATION_DIR / "translate.sh"

# === In-memory status (per-process) ===
_status: dict = {
    "task_id": None,
    "state": "idle",  # idle / queued / running / done / failed
    "filename": None,
    "engine": None,
    "started_at": None,
    "finished_at": None,
    "stdout": [],
    "stderr": [],
    "exit_code": None,
    "output_files": [],
}
_status_lock = threading.Lock()


def list_docx(folder: str) -> list[dict]:
    """列出 folder 內所有 .docx 檔, 包含 mtime 跟 size"""
    p = Path(folder)
    if not p.exists():
        return []
    files = []
    for f in sorted(p.glob("*.docx")):
        try:
            st = f.stat()
            files.append({
                "name": f.name,
                "size_kb": round(st.st_size / 1024, 1),
                "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "path": str(f),
            })
        except OSError:
            continue
    return files


def get_status() -> dict:
    """Return current translation status"""
    with _status_lock:
        return dict(_status)


def reset_status():
    with _status_lock:
        _status.update({
            "task_id": None,
            "state": "idle",
            "filename": None,
            "engine": None,
            "started_at": None,
            "finished_at": None,
            "stdout": [],
            "stderr": [],
            "exit_code": None,
            "output_files": [],
        })


def _run_translation(filename: str, engine: str, outbox: str, inbox: str):
    """Background runner"""
    global _status
    task_id = f"task_{int(time.time())}"
    
    with _status_lock:
        _status["task_id"] = task_id
        _status["state"] = "running"
        _status["filename"] = filename
        _status["engine"] = engine
        _status["started_at"] = datetime.now().isoformat()
        _status["stdout"] = []
        _status["stderr"] = []
        _status["exit_code"] = None
        _status["output_files"] = []
    
    # Use translate_engines_v2.py (2026-07-26 紅字 only + 保留 XML)
    # args: filename [engine|all]
    v2_script = TRANSLATION_DIR / "translate_engines_v2.py"
    cmd = ["python3", str(v2_script), filename, engine if engine else "all"]
    
    # env: TRANSLATION_OUTBOX 可自訂
    env = os.environ.copy()
    if outbox:
        env["TRANSLATION_OUTBOX"] = outbox
    if inbox:
        env["TRANSLATION_INBOX"] = inbox
    
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(TRANSLATION_DIR),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        
        # 即時 capture output
        for line in proc.stdout:
            with _status_lock:
                _status["stdout"].append(line.rstrip())
        for line in proc.stderr:
            with _status_lock:
                _status["stderr"].append(line.rstrip())
        
        proc.wait()
        exit_code = proc.returncode
        
        with _status_lock:
            _status["exit_code"] = exit_code
            _status["finished_at"] = datetime.now().isoformat()
            _status["state"] = "done" if exit_code == 0 else "failed"
            
            # 找 output files (default done/ 一定檢查, user outbox 額外檢查)
            basename = Path(filename).stem
            search_dirs = [
                TRANSLATION_DIR / "done",  # 預設 done (v2 寫這)
            ]
            if outbox:
                search_dirs.append(Path(outbox))
            
            for out_dir in search_dirs:
                if not out_dir.exists():
                    continue
                for ext_pattern in [f"{basename}_google.docx", f"{basename}_minimax.docx", f"{basename}_google_FAILED.docx"]:
                    candidate = out_dir / ext_pattern
                    if candidate.exists() and str(candidate) not in _status["output_files"]:
                        _status["output_files"].append(str(candidate))
    
    except Exception as e:
        with _status_lock:
            _status["state"] = "failed"
            _status["stderr"].append(f"Exception: {e}")
            _status["finished_at"] = datetime.now().isoformat()
            _status["exit_code"] = -1


def start_translation(filename: str, engine: str, outbox: str = "", inbox: str = "") -> dict:
    """Start translation in background thread. Returns immediately."""
    with _status_lock:
        if _status["state"] in ("queued", "running"):
            return {
                "ok": False,
                "error": f"已有 task 進行中 ({_status['filename']}, {_status['engine']})",
            }
    
    # Reset
    reset_status()
    
    # Start thread
    thread = threading.Thread(
        target=_run_translation,
        args=(filename, engine, outbox, inbox),
        daemon=True,
    )
    thread.start()
    
    with _status_lock:
        _status["state"] = "queued"
    
    return {
        "ok": True,
        "task_id": _status["task_id"],
        "filename": filename,
        "engine": engine,
    }
