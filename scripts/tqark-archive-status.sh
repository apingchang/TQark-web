#!/bin/bash
# TQark-web StudyArk archive status check
# 
# 用法:
#   tqark-archive-status           # 預設概覽
#   tqark-archive-status --full    # 完整 log
#   tqark-archive-status --live    # 持續追蹤 (類似 tail -f)

set -e

export ARCHIVE_DIR=/mnt/my_book/考題收集
LOG_FILE=$ARCHIVE_DIR/logs/archive.log
STATUS_FILE=$ARCHIVE_DIR/state/archive_status.json
ACCOUNT_STATUS=$ARCHIVE_DIR/state/account_status.json

show_summary() {
    echo "═══════════════════════════════════════════════════════"
    echo "  TQark-web StudyArk Archive Status"
    echo "  $(date '+%Y-%m-%d %H:%M:%S %Z')"
    echo "═══════════════════════════════════════════════════════"
    echo ""
    
    # Fileids 累計
    if [ -f "$STATUS_FILE" ]; then
        python3 -c "
import json, sys
from pathlib import Path

status_file = Path('$STATUS_FILE')
account_file = Path('$ACCOUNT_STATUS')

# Main status
d = json.loads(status_file.read_text())
total = d.get('total_collected', 0)
last = d.get('last_run', 'never')
result = d.get('last_run_result', 'unknown')

# StudyArk total
total_file = Path('$ARCHIVE_DIR/state/studyark_total.json')
studyark_total = 20158
if total_file.exists():
    try:
        sd = json.loads(total_file.read_text())
        studyark_total = sd.get('last_count', 20158)
    except: pass

# Account status 【2026-07-24 改】讀 cooldown 而不是舊的 exhausted list
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

exhausted_with_recovery = []  # list of (name, minutes_left_until_recovery)
if account_file.exists():
    try:
        ad = json.loads(account_file.read_text())
        TZ_TAIPEI = ZoneInfo('Asia/Taipei')
        today = datetime.now(TZ_TAIPEI).date().isoformat()
        # 舊格式 (exhausted list)
        if 'exhausted' in ad and isinstance(ad['exhausted'], list):
            exhausted_with_recovery = [(n, None) for n in ad['exhausted']]
        # 新格式 (cooldown dict)
        elif ad.get('date') == today and 'cooldown' in ad:
            for acc_name, until_str in ad['cooldown'].items():
                try:
                    until = datetime.fromisoformat(until_str)
                    now = datetime.now(TZ_TAIPEI)
                    if now < until:
                        minutes_left = max(0, int((until - now).total_seconds() / 60))
                        exhausted_with_recovery.append((acc_name, minutes_left))
                except ValueError:
                    pass
    except Exception as e:
        pass

# Disk PDF count + 5 類分類 【2026-07-24 新】
# 用 os.walk 跟 web UI (_scan_archive_counts) 一樣邏輯, 避免 find -path 複雜
import subprocess
import sys as _sys
import os as _os
ARCHIVE_DIR_PATH = _os.environ.get('ARCHIVE_DIR', '/mnt/my_book/考題收集')

pdf_count = 0
primary_count = 0
junior_count = 0
senior_count = 0
cap_count = 0
ceec_count = 0

SKIP_TOP_DIRS = ('state', 'logs')
try:
    for _dirpath, _dirnames, _filenames in _os.walk(ARCHIVE_DIR_PATH):
        if _dirpath == ARCHIVE_DIR_PATH:
            _dirnames[:] = [d for d in _dirnames if d not in SKIP_TOP_DIRS]
        # 【2026-07-24 新】ceec/_generic 不算入 5 類 (跟 web UI 一致)
        if '_generic' in _dirpath.split(_os.sep):
            _dirnames[:] = []
            continue
        for _fname in _filenames:
            if not _fname.endswith('.pdf'):
                continue
            pdf_count += 1
            _full = _os.path.join(_dirpath, _fname)
            _rel = _os.path.relpath(_full, ARCHIVE_DIR_PATH)
            _parts = _rel.split(_os.sep)
            if _parts and _parts[0] == 'cap_exam':
                cap_count += 1
                continue
            if _parts and _parts[0] == 'ceec':
                ceec_count += 1
                continue
            for _p in _parts[:-1]:
                if _p == '國小':
                    primary_count += 1
                    break
                elif _p == '國中':
                    junior_count += 1
                    break
                elif _p == '高中':
                    senior_count += 1
                    break
except OSError as _e:
    print(f'  [DEBUG] os.walk failed: {_e}', file=_sys.stderr)

# 學段分布 (paper only, 全站 - County-aware: county 下的 paper/*.pdf 都要算)
paper_count = {'國小': 0, '國中': 0, '高中': 0}
for level in paper_count:
    try:
        n = subprocess.check_output(
            ['find', ARCHIVE_DIR_PATH, '-path', f'*/{level}/*/paper/*.pdf'],
            stderr=subprocess.DEVNULL
        ).decode().count('\n')
        paper_count[level] = n
    except: pass

pct = (total / studyark_total * 100) if studyark_total else 0
remaining = studyark_total - total
days_left = remaining / 120  # 4 帳號 × 30/day

print(f'📊 累計進度')
print(f'  已抓 fileids:    {total:,} / {studyark_total:,}  ({pct:.2f}%)')
print(f'  Disk 上 PDFs:    {pdf_count} 個')
print(f'  學段分布:        國小 {paper_count[\"國小\"]} / 國中 {paper_count[\"國中\"]} / 高中 {paper_count[\"高中\"]}')
print(f'')
print(f'⏰ 預估完成 (4 帳號輪流)')
print(f'  每天抓取:        ~120 fileids/day')
print(f'  預估剩餘:        {remaining:,} 個 fileids')
print(f'  預估完成:        ~{days_left:.0f} 天 (~{int(days_left/30.4)} 個月)')
print(f'')
print(f'🕐 上次執行')
print(f'  時間:            {last}')
print(f'  結果:            {result}')
print(f'')
print(f'👥 帳號狀態')
if exhausted_with_recovery:
    parts = []
    for name, mins in exhausted_with_recovery:
        if mins is None:
            parts.append(f'{name}')
        elif mins >= 60:
            parts.append(f'{name} ({mins // 60}h{mins % 60}m)')
        else:
            parts.append(f'{name} ({mins}m)')
    print(f'  Exhausted:       ' + ', '.join(parts))
    # Show earliest recovery time
    rec_times = [(n, datetime.fromisoformat(ad['cooldown'][n])) for n, m in exhausted_with_recovery if m is not None]
    if rec_times:
        earliest = min(rec_times, key=lambda x: x[1])
        print(f'  最早恢復:        {earliest[0]} @ {earliest[1].strftime(\"%H:%M:%S\")}')
else:
    print(f'  Exhausted:       (無 exhausted)')
print(f'')
print(f'📚 PDF 分布 (5 類)')
print(f'  📒 小學 (國小):  {primary_count}')
print(f'  📗 國中:        {junior_count}')
print(f'  📘 高中:        {senior_count}')
print(f'  📙 會考 (CAP):  {cap_count}')
print(f'  📕 大考 (CEEC): {ceec_count}')
print(f'  總計:           {primary_count + junior_count + senior_count + cap_count + ceec_count}')
print(f'')
"
    else
        echo "❌ Status file not found: $STATUS_FILE"
    fi
    
    # 最近 5 批
    echo "─────────────────────────────────────────────────────────"
    echo "📜 最近 5 批 batch"
    echo "─────────────────────────────────────────────────────────"
    grep "Archive task done\|rate_limited" "$LOG_FILE" 2>/dev/null | tail -5
    
    echo ""
}

show_full_log() {
    echo "═══════════════════════════════════════════════════════"
    echo "  📜 完整 archive log (最近 60 行)"
    echo "═══════════════════════════════════════════════════════"
    tail -60 "$LOG_FILE"
}

show_live() {
    echo "📡 持續追蹤 (Ctrl+C 離開)..."
    tail -f "$LOG_FILE"
}

# Main
case "${1:-}" in
    --full|-f)
        show_summary
        show_full_log
        ;;
    --live|-l)
        show_live
        ;;
    --help|-h)
        echo "用法: tqark-archive-status [option]"
        echo ""
        echo "  (無參數)    顯示狀態概覽"
        echo "  --full      顯示完整最近 log"
        echo "  --live      持續追蹤 (tail -f)"
        ;;
    *)
        show_summary
        ;;
esac