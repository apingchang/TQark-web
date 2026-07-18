# TQark Web — 國中考古題下載

> 將 StudyArk(全國中小學題庫網 - 學習方舟)的國中段考考古題,
> 透過**私有 invite-only** 網頁應用,提供給受邀使用者下載。

[![Status](https://img.shields.io/badge/status-design-blueviolet)]()
[![License](https://img.shields.io/badge/license-MIT-green)]()
[![Python](https://img.shields.io/badge/python-3.12+-blue)]()
[![Users](https://img.shields.io/badge/users-50+-orange)]()

---

## 🌟 這是做什麼的?

讓 William 跟幾位同事可以透過 Google 一鍵登入,簡單點幾下就下載到國中各校各科的考古題 PDF。

**給家長同事一個免費、安全、不對外公開的工具**。

---

## ✨ 功能(規劃中)

- 🔐 **Google 登入** — 一鍵,免記密碼
- 👥 **半開放註冊** — 申請後由 admin 審核
- 🔍 **多條件搜尋** — 年級、學年度、學期、領域、版本,任你組合
- 📄 **自動命名** — 下載的 PDF 自動用統一格式命名
- ⚡ **快取加速** — 下載過的試卷不用重複從來源拉
- 🚦 **流量保護** — Rate limit 防止濫用
- 🆓 **完全免費** — 不登入、不收費、不追蹤、不公開

---

## 🚀 使用流程

### 對 User(同事)

**第一次**:
1. 打開 `https://just4fun.myiphost.com:8443/`
2. 點「Sign in with Google」
3. 選你的 Google 帳號 → 同意授權
4. 回來填寫「為什麼想加入」訊息 → 送出申請
5. 看到「申請已送出,等待 admin 核准」(通常 1-3 天處理)

**申請通過後**:
1. 重新登入 → 直接看到搜尋介面
2. 填條件(例:八年級 / 113 學年度 / 下學期 / 國文 / 翰林)
3. 從結果挑學校 → 點下載就拿到 PDF

**被停用**:
- 看到「帳號被停用,聯絡 admin」訊息 → 找 William

### 對 Admin(William)

1. 第一次:用 `.env` 的 `ADMIN_EMAILS` 設你的 email → 用該 Google 帳號登入 → 自動變 admin
2. 進 `/admin` 看到 dashboard
3. Pending User 列表 → 點 Approve / Reject
4. 所有 User 列表 → 看狀態 / Block / Promote
5. Audit Log → 看誰做了什麼
6. Stats → 看流量

---

## 🛠 開發者 / 維運者

### 環境需求

| 工具 | 版本 |
|------|------|
| Python | 3.12+ |
| Caddy | 2.7+ |
| systemd | (Linux 內建) |

### 開發環境(本機)

```bash
# 1. clone
git clone git@github.com:apingchang/TQark-web.git
cd TQark-web

# 2. 設定 backend
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# 3. 設定 .env
cp .env.example .env
# 編輯 .env(至少填 GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET + ADMIN_EMAILS + JWT_SECRET)

# 4. 起服務
uvicorn app.main:app --reload --port 8000

# 5. 開瀏覽器(注意:Google OAuth 設的 redirect URI 也要對應)
# 本機開發建議用 http://localhost:8000,Google Cloud Console 要加這個 origin
```

### Production 部屬

完整 step-by-step 在 [`docs/deployment.md`](docs/deployment.md)。

```bash
# 1. clone 到家裡主機
cd /home/aping
git clone git@github.com:apingchang/TQark-web.git
cd TQark-web

# 2. 建資料目錄
mkdir -p data/cookies data/pdfs data/db data/logs
chmod 700 data/cookies

# 3. 設定 backend venv + 套件
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# 4. 設定 .env
cp .env.example .env
nano .env  # 填 Google OAuth + ADMIN_EMAILS + JWT_SECRET
chmod 600 .env

# 5. systemd service
sudo cp deploy/systemd/tqark-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable tqark-web
sudo systemctl start tqark-web

# 6. Caddy
sudo cp deploy/caddy/Caddyfile /etc/caddy/
sudo systemctl enable caddy
sudo systemctl restart caddy

# 7. 驗證
curl -sI https://just4fun.myiphost.com:8443/health
```

---

## 📁 專案結構

```
TQark-web/
├── README.md                  ← 你正在看這個
├── CHANGELOG.md
├── CONTRIBUTING.md
├── LICENSE
├── SETUP_GITHUB.sh
├── docs/
│   ├── architecture.md        ← 系統架構、schema
│   ├── deployment.md          ← Caddy + 8443 + systemd step-by-step
│   ├── google-oauth-setup.md  ← Google Cloud Console step-by-step
│   ├── user-management.md     ← Admin / User SOP
│   ├── security-model.md      ← Threat model + 事故 SOP
│   └── cookie-maintenance.md  ← StudyArk cookies 維護
└── backend/                   ← (Phase 1 才會有)
    └── app/
        ├── main.py
        ├── routes/
        ├── services/
        └── models/
```

---

## 🛠 技術棧

| 層 | 選用 |
|----|------|
| Backend | Python 3.12 + FastAPI + SQLAlchemy + Pydantic |
| Auth | Google OAuth 2.0 + JWT |
| Frontend | 純 HTML + Vanilla JS + Tailwind CDN |
| DB | SQLite |
| Reverse proxy | Caddy(自動 HTTPS) |
| Process manager | systemd |
| Scraper | Playwright(async) |

---

## ⚖️ 免責聲明

- 本站所有考古題來源為 [StudyArk 全國中小學題庫網](https://www.studyark.org/)
- 試卷版權歸原作者及各校所有
- **僅供學術與個人學習使用,請勿用於營利或侵權用途**
- 本站為私有服務,僅供受邀使用者使用,不對外公開
- 如版權方要求下架,請聯絡 William,我們會盡快處理

---

## 📝 版本

詳見 [`CHANGELOG.md`](CHANGELOG.md)。

目前狀態:**設計階段**,尚未開工 coding。

---

## 🌐 中英對照

| 中文 | English |
|------|---------|
| 國中考古題下載站 | TQark Web - Junior High Exam Archive |
| 搜尋 | Search |
| 下載 | Download |
| 申請 access | Request access |
| 管理員 | Admin |
| 學年度 | Academic Year |
| 上/下學期 | First/Second Semester |
| 段考 | Unit Exam |
| 期中考 / 期末考 | Midterm / Final Exam |
| 版本 | Edition (Hanlin / Kang Hsuan / Nan Yi) |