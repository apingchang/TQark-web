#!/bin/bash
# TQark Web — GitHub repo 建置 + push 一鍵腳本
# 用法:
#   1. 去 https://github.com/new 建一個 repo(見下方說明)
#   2. 回到 terminal 跑這個 script
#
# 預期 repo 設定:
#   - Owner:    apingchang  (或 ameow-cpu,你選)
#   - Name:     TQark-web
#   - Public:   ✅
#   - 不要勾任何 "Add README / .gitignore / License" (我們本地都有了)
#   - 點 "Create repository"

set -e

REPO_DIR="/home/aping/.openclaw/workspace/TQark-web-plan"

cd "$REPO_DIR"

# 問 owner
echo "GitHub owner? (預設 apingchang,直接 Enter 用預設)"
read -r OWNER
OWNER=${OWNER:-apingchang}

# 設定 remote
echo ""
echo "Setting remote to git@github.com:${OWNER}/TQark-web.git ..."
git remote remove origin 2>/dev/null || true
git remote add origin "git@github.com:${OWNER}/TQark-web.git"

# Push
echo ""
echo "Pushing to origin main ..."
git push -u origin main

echo ""
echo "✅ Done!"
echo "🌐 https://github.com/${OWNER}/TQark-web"