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


def _run_translation(staging_path: str, original_filename: str, engine: str):
    """Background runner
    Args:
        staging_path: 絕對路徑到 staging .docx
        original_filename: 原始檔名 (for output naming)
        engine: google / minimax / all
    """
    global _status
    task_id = f"task_{int(time.time())}"
    
    with _status_lock:
        _status["task_id"] = task_id
        _status["state"] = "running"
        _status["filename"] = original_filename
        _status["engine"] = engine
        _status["started_at"] = datetime.now().isoformat()
        _status["stdout"] = []
        _status["stderr"] = []
        _status["exit_code"] = None
        _status["output_files"] = []
        _status["staging_path"] = staging_path
    
    # 為了讓 translate_engines_v2.py 能找到檔案, 我們把 staging 檔案 link 到 translation/inbox
    inbox_dir = TRANSLATION_DIR / "inbox"
    inbox_dir.mkdir(exist_ok=True)
    inbox_filename = original_filename  # 用原檔名讓 v2 script 能找
    inbox_path = inbox_dir / inbox_filename
    
    # 複製 (避免 link 跨裝置失敗)
    try:
        import shutil
        shutil.copy(staging_path, inbox_path)
    except Exception as e:
        with _status_lock:
            _status["state"] = "failed"
            _status["stderr"].append(f"Failed to stage file: {e}")
            _status["finished_at"] = datetime.now().isoformat()
            _status["exit_code"] = -1
        return
    
    # Use translate_engines_v2.py (用 venv python 確保 lxml 可用)
    import sys as _sys
    venv_python = _sys.executable  # 通常是 .venv/bin/python
    v2_script = TRANSLATION_DIR / "translate_engines_v2.py"
    cmd = [venv_python, str(v2_script), inbox_filename, engine if engine else "all"]
    
    # venv 不 include system site-packages, lxml 在 user site-packages
    # 加 PYTHONPATH 確保 subprocess 能 import lxml
    env = os.environ.copy()
    env["PYTHONPATH"] = "/home/aping/.local/lib/python3.12/site-packages:" + env.get("PYTHONPATH", "")

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
            
            # 找 output files (根據 user 選的 engine)
            basename = Path(original_filename).stem
            if engine == "all":
                engines_to_find = ["google", "minimax"]
            else:
                engines_to_find = [engine]
            
            for eng in engines_to_find:
                candidate = TRANSLATION_DIR / "done" / f"{basename}_{eng}.docx"
                if candidate.exists():
                    _status["output_files"].append(str(candidate))
    
    except Exception as e:
        with _status_lock:
            _status["state"] = "failed"
            _status["stderr"].append(f"Exception: {e}")
            _status["finished_at"] = datetime.now().isoformat()
            _status["exit_code"] = -1
    finally:
        # 清理 staging inbox (不要污染原本的 inbox)
        try:
            inbox_path.unlink(missing_ok=True)
        except Exception:
            pass


def start_translation(staging_path: str, original_filename: str, engine: str) -> dict:
    """Start translation in background thread. Returns immediately.
    
    Args:
        staging_path: Absolute path to uploaded .docx in staging area
        original_filename: Original filename (for display + output naming)
        engine: google / minimax / all
    """
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
        args=(staging_path, original_filename, engine),
        daemon=True,
    )
    thread.start()
    
    with _status_lock:
        _status["state"] = "queued"
    
    return {
        "ok": True,
        "task_id": _status["task_id"],
        "filename": original_filename,
        "engine": engine,
    }

# === Staging area for uploaded files ===
STAGING_DIR = Path("/tmp/jp_staging")
STAGING_DIR.mkdir(exist_ok=True)


def save_uploaded_file(file_bytes: bytes, original_filename: str) -> Path:
    """Save uploaded .docx to staging, return absolute path"""
    import shutil
    
    # Sanitize filename (avoid path traversal)
    safe_name = Path(original_filename).name  # strip dir components
    if not safe_name.endswith(".docx"):
        safe_name += ".docx"
    
    # Add timestamp to avoid collision
    from datetime import datetime as _dt
    ts = _dt.now().strftime("%Y%m%d_%H%M%S")
    name_parts = safe_name.rsplit(".", 1)
    if len(name_parts) == 2:
        unique_name = f"{name_parts[0]}_{ts}.{name_parts[1]}"
    else:
        unique_name = f"{safe_name}_{ts}"
    
    staging_path = STAGING_DIR / unique_name
    staging_path.write_bytes(file_bytes)
    return staging_path


def get_output_path(original_filename: str, engine: str = "google") -> Path | None:
    """Find translated output file.
    Args:
        original_filename: user uploaded filename (e.g. "260795.docx")
        engine: google / minimax
    Returns:
        Path to output .docx or None
    """
    basename = Path(original_filename).stem
    for ext_pattern in [f"{basename}_google.docx", f"{basename}_minimax.docx"]:
        candidate = TRANSLATION_DIR / "done" / ext_pattern
        if candidate.exists():
            return candidate
    return None
