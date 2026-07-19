#!/bin/bash
# TQark-web backend startup script
# 用 .venv 直接執行 uvicorn,不透過 uv(避免 uv 自動檢查 deps)

set -e

cd /home/aping/MyProjects/TQark-web/backend

exec .venv/bin/python -m uvicorn app.main:app \
  --host 127.0.0.1 \
  --port 8000 \
  --workers 2 \
  --log-level info \
  --no-access-log