# TQark Web — 國中考古題下載站

> 將 StudyArk(全國中小學題庫網 - 學習方舟)上的國中段考考古題,
> 透過網頁提供給需要的人下載。

[![Status](https://img.shields.io/badge/status-design-blueviolet)]()
[![License](https://img.shields.io/badge/license-MIT-green)]()
[![Python](https://img.shields.io/badge/python-3.12+-blue)]()

---

## 🌟 這是做什麼的?

讓任何人都能透過瀏覽器,簡單幾個點擊就下載到國中各校各科的考古題 PDF。

**給家長、老師、學生一個不用 Google 帳號、不用爬蟲就能拿到考古題的地方。**

---

## ✨ 功能(規劃中)

- 🔍 **多條件搜尋** — 年級、學年度、學期、領域、版本,任你組合
- 📄 **自動命名** — 下載的 PDF 自動用統一格式命名,直接歸檔
- ⚡ **快取加速** — 下載過的試卷不用重複從來源拉
- 🚦 **流量保護** — Rate limit 防止濫用
- 🆓 **完全免費** — 不登入、不收費、不追蹤

---

## 🚀 快速開始(之後開發完會更新)

### 使用者

1. 打開 https://studyark.example.com(域名待定)
2. 填搜尋條件(例:八年級 / 113 學年度 / 下學期 / 國文 / 翰林)
3. 從結果列表挑學校,點「下載」就拿到 PDF

### 開發者 / 維運者

詳細部屬請見 [`docs/deployment.md`](docs/deployment.md)。

```bash
# 1. clone
git clone https://github.com/apingchang/TQark-web.git
cd TQark-web

# 2. 設定環境變數
cp backend/.env.example backend/.env
# 編輯 .env,填上 StudyArk cookies 路徑

# 3. 起服務
docker compose up -d

# 4. 開瀏覽器看
open http://localhost:8000
```

---

## 📁 專案結構

```
TQark-web/
├── README.md                  ← 你正在看這個
├── docs/                      ← 設計、架構、部屬、維運文件
├── backend/                   ← Python FastAPI 服務
├── frontend/                  ← 純 HTML + Tailwind 介面
├── deploy/                    ← Docker / Nginx / Cloudflare Tunnel
└── .github/workflows/         ← CI/CD
```

詳細請見 [`PROJECT_PLAN.md`](PROJECT_PLAN.md)。

---

## 🛠 技術棧

| 層 | 選用 | 原因 |
|----|------|------|
| 後端 | Python 3.12 + FastAPI + Playwright | async 友善、跟既有 scraper 整合順 |
| 前端 | 純 HTML + Vanilla JS + Tailwind CDN | 不需要 build step、3 頁就夠 |
| 快取 | SQLite + 本地檔案 | 小流量用不上 Redis |
| 部署 | Docker Compose + Cloudflare Tunnel | 免費 HTTPS、隱藏 IP |

---

## ⚖️ 免責聲明

- 本站所有考古題來源為 [StudyArk 全國中小學題庫網](https://www.studyark.org/)
- 試卷版權歸原作者及各校所有
- **僅供學術與個人學習使用,請勿用於營利或侵權用途**
- 如版權方要求下架,請來信通知,我們會盡快處理

---

## 📝 版本

詳見 [`CHANGELOG.md`](CHANGELOG.md)。

目前狀態:**設計階段**,尚未開工 coding。

---

## 🤝 貢獻

詳見 [`CONTRIBUTING.md`](CONTRIBUTING.md)。

(目前主要是 William 個人 + AI 協作的私人專案,完成後會考慮開放。)

---

## 📞 聯絡

- GitHub Issues: https://github.com/apingchang/TQark-web/issues
- Email: (之後填上)

---

## 🌐 中英對照

| 中文 | English |
|------|---------|
| 國中考古題下載站 | TQark Web - Junior High Exam Archive Downloader |
| 搜尋 | Search |
| 下載 | Download |
| 學年度 | Academic Year |
| 上/下學期 | First/Second Semester |
| 段考 | Unit Exam |
| 期中考 / 期末考 | Midterm / Final Exam |
| 版本 | Edition (Hanlin / Kang Hsuan / Nan Yi) |