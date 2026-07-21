# TQark Web — 專案設計書

> 將 StudyArk(全國中小學題庫網 - 學習方舟)的國中段考考古題,
> 透過 **私有 invite-only 網頁應用** 提供給受邀的使用者下載。

**狀態**: 設計階段(尚未開工 coding)
**作者**: William Chang
**協作者**: 夥計 (OpenClaw main agent)
**日期**: 2026-07-18 (3rd revision)

---

## 1. 目標

### 1.1 解決的問題
William 想跟幾位有小孩的同事分享考古題下載工具,需要一個**簡單、安全、不對外公開**的網頁應用。

### 1.2 目標使用者
- **主要**: William 自己 + 約 50+ 位同事(家裡有國中生的家長)
- **不會是**: 一般民眾、營利單位(法律/規模考量)

### 1.3 核心價值
| 對誰 | 解決什麼 |
|------|---------|
| William | 「我自己想快速抓考古題,順便分享給同事」 |
| 同事 | 「免費、Google 一鍵登入、就能拿到 PDF,不用學技術」 |
| William(法律面) | 「可控 user 名單、可隨時 revoke、有 audit log」 |

---

## 2. 功能範圍

### 2.1 一定要有(P0)
- [ ] 公開 landing page(說明用途 + Sign in with Google 按鈕)
- [ ] Google OAuth 登入
- [ ] User 申請 access(填原因 → pending)
- [ ] Admin 審核 dashboard(看 pending → approve/reject)
- [ ] Approved user 才能看到搜尋介面
- [ ] 搜尋:年級 / 學年度 / 學期 / 領域 / 科目 / 版本
- [ ] 結果列表 + 下載按鈕(試卷 + 答案卷分開)
- [ ] PDF 直接從 StudyArk 抓(透過 Playwright)
- [ ] 本地 PDF cache(同一份試卷只抓一次)
- [ ] SQLite metadata cache(降低 StudyArk 請求量)
- [ ] `/health` endpoint(監控)
- [ ] Admin 介面:user 管理 + audit log + 流量統計
- [ ] Rate limit(防濫用、StudyArk ban 防護)

### 2.2 應該要有(P1)
- [ ] 完整 audit log(誰做了什麼)
- [ ] Block / unblock user
- [ ] Admin promote/demote
- [ ] Welcome email(可選 Phase 1)
- [ ] Cookie 過期監控 + alert
- [ ] Log rotation(避免塞爆磁碟)

### 2.3 之後再加(P2)
- [ ] 我的最愛學校
- [ ] 批次下載(多選打包 zip)
- [ ] 「熱門下載 Top 10」widget
- [ ] 公告系統(admin 可發佈訊息給所有 user)
- [ ] i18n 雙語切換

### 2.4 不要做
- 廣告(AdSense)— 私人服務,不需要
- 付費功能
- 公開註冊(只能透過 invite 或半開放申請)
- 評論 / 評分
- 上傳功能

---

## 3. 技術選型(已拍板)

| 層 | 選用 | 理由 |
|----|------|------|
| **Backend** | Python 3.12 + FastAPI | async 友善、跟既有 scraper 整合順 |
| **Auth** | Google OAuth 2.0 + JWT + slowapi | 免費、可靠、有 2FA |
| **Frontend** | 純 HTML + Vanilla JS + Tailwind CDN | 不用 build step、簡單 |
| **DB** | SQLite | < 100 user 完全勝任、單檔好備份 |
| **Reverse proxy** | Caddy | 自動 HTTPS、config 簡單 |
| **Process manager** | systemd | 跟 OpenClaw 一致 |
| **PDF cache** | 本地檔案系統 `./data/pdfs/` | 之後量大再搬 R2 |
| **Deployment** | 家裡主機 + DDNS | 已有 + 不花錢 + 可控 |

**為什麼不選 Docker**:簡單部署 + 跟 OpenClaw 部署方式一致。
**為什麼不選 Redis**:50+ 人流量低,SQLite + in-process 夠用。

---

## 4. 系統架構

```
[User 同事瀏覽器]
   ↓ https://just4fun.myiphost.com:8443
[家裡 Router]
   ├─ Port Forwarding: 8443 → 192.168.50.31:8443
   ↓
[GreenHouseUbuntu Linux 主機]
   ├─ :8443  Caddy 2.7
   │       ├─ 自動 HTTPS (Let's Encrypt)
   │       ├─ Reverse proxy → 127.0.0.1:8000
   │       ├─ Security headers (CSP, HSTS, X-Frame-Options)
   │       └─ Rate limit (Caddy 內建)
   │
   ├─ :8000  FastAPI (systemd service, **127.0.0.1 only** ← 只 listen loopback,不出 Linux 主機)
   │       ├─ /auth/google/login (OAuth flow)
   │       ├─ /auth/google/callback
   │       ├─ /auth/me (看自己狀態)
   │       ├─ /api/access-requests (User 申請 access)
   │       ├─ /api/search (approved user only)
   │       ├─ /api/download/{id}
   │       ├─ /admin/* (admin only)
   │       └─ /health
   │
   ├─ SQLite DB
   │       ├─ users (id, email, name, role, status, google_id, ...)
   │       ├─ search_cache (cache_key, results_json, expires_at, ...)
   │       ├─ exam_metadata (exam_id, school, grade, year, ...)
   │       └─ audit_log (user_id, action, target, ip_hash, ...)
   │
   ├─ PDF cache → ./data/pdfs/
   ├─ StudyArk cookies → ./data/cookies/studyark_cookies.json (chmod 600)
   └─ Logs → ./data/logs/

[外部服務]
   ├─ Google OAuth → https://accounts.google.com/
   └─ StudyArk → https://www.studyark.org/ (透過 Playwright + cookies)
```

### 4.0 Port 規劃(關鍵!容易誤讀)

**對外只開一個 port**:`8443`(HTTPS,已經在 router 設好 port forwarding)

| Port | 服務 | 誰能 access | 要設 port forwarding 嗎? |
|------|------|-------------|--------------------------|
| **8443** | Caddy | 任何人(對外) | ✅ **要**(已設) |
| 8000 | FastAPI | **只有 Linux 本機**(127.0.0.1) | ❌ **不用** |
| 443 | (其他服務) | 對外 | 看 router |

**FastAPI 為什麼用 8000 但不需要 port forwarding**:
- systemd 啟動 uvicorn 時加 `--host 127.0.0.1 --port 8000`
- `127.0.0.1` = loopback = **只有 Linux 本機能連**
- Caddy 用 `reverse_proxy 127.0.0.1:8000` 從內部 call FastAPI
- 對外使用者 → router → :8443 → Caddy → :8000 → FastAPI
- **攻擊者沒辦法直接打 FastAPI**(只看到 Caddy)

詳細 systemd unit 設定見 [`docs/deployment.md`](docs/deployment.md) Step 6。

### 4.1 關鍵流程

**User 申請 + Admin approve**:
```
User → /api/access-requests (Google 登入後)
  → 寫 users table (status=pending)
  → User 看到「等待中」

Admin → /admin/users?status=pending
  → 看清單 → 點 Approve
  → users.status = approved, approved_at = now(), approved_by = admin.id
  → 寫 audit_log (action=user_approve)

User 下次登入 → status=approved → 看到搜尋介面
```

**Search + Download**:
```
User → /api/search?grade=8&year=113&semester=2&subject=國文&version=翰林
  → FastAPI 驗 JWT(approved user only)
  → Rate limit check
  → 查 SQLite metadata cache
  → cache hit → 回 JSON
  → cache miss → Playwright 抓 StudyArk → 寫 cache → 回 JSON
  → 寫 audit_log (action=search)

User → /api/download/{exam_id}
  → 查 PDF cache
  → hit → FileResponse
  → miss → Playwright 抓 → 存 cache → FileResponse
  → 寫 audit_log (action=download)
```

---

## 5. DB Schema

```sql
-- Users
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    picture_url TEXT,
    google_id TEXT UNIQUE NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',  -- 'user' | 'admin'
    status TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'approved' | 'blocked'
    application_message TEXT,
    approved_at TIMESTAMP,
    approved_by INTEGER,
    blocked_at TIMESTAMP,
    blocked_reason TEXT,
    last_login_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (approved_by) REFERENCES users(id)
);

-- Search cache
CREATE TABLE search_cache (
    cache_key TEXT PRIMARY KEY,
    query_params TEXT NOT NULL,
    results_json TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP NOT NULL,
    hit_count INTEGER DEFAULT 0
);

-- Exam metadata
CREATE TABLE exam_metadata (
    exam_id TEXT PRIMARY KEY,
    school_name TEXT NOT NULL,
    grade INTEGER NOT NULL,
    year INTEGER NOT NULL,
    semester INTEGER NOT NULL,
    domain TEXT,
    subject TEXT NOT NULL,
    exam_number INTEGER,
    exam_type TEXT,
    version TEXT,
    has_answer BOOLEAN DEFAULT FALSE,
    studyark_url TEXT NOT NULL,
    first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- PDF cache index
CREATE TABLE pdf_cache (
    exam_id TEXT PRIMARY KEY,
    file_path TEXT NOT NULL,
    size_bytes INTEGER,
    cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_accessed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (exam_id) REFERENCES exam_metadata(exam_id)
);

-- Audit log
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    action TEXT NOT NULL,
    target TEXT,
    metadata_json TEXT,
    ip_hash TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

-- Indices
CREATE INDEX idx_users_status ON users(status);
CREATE INDEX idx_users_email ON users(email);
CREATE INDEX idx_search_expires ON search_cache(expires_at);
CREATE INDEX idx_exam_lookup ON exam_metadata(grade, year, semester, subject, version);
CREATE INDEX idx_audit_user ON audit_log(user_id);
CREATE INDEX idx_audit_created ON audit_log(created_at);
CREATE INDEX idx_audit_action ON audit_log(action);
```

---

## 6. 命名規則

### 6.1 Repo
- GitHub: `apingchang/TQark-web`
- 顯示名稱: **TQark Web**(英) / **國中考古題下載**(中)

### 6.2 下載檔名(沿用 William 既有格式)
```
{學校名} {年級} {學年度} {學期} {領域} {科目} {第幾次段考} {期中考/期末考} {版本} 試卷.pdf
{學校名} {年級} {學年度} {學期} {領域} {科目} {第幾次段考} {期中考/期末考} {版本} 試卷_答案卷.pdf
```

---

## 7. 安全 / 法律 / 隱私

### 7.1 法律風險評估(2026-07-18 更新)
**這個設計 vs 公開服務**:

| 維度 | 公開服務 | TQark Web(私有 invite-only) |
|------|---------|----------------------------|
| 使用者 | 不特定多數人 | 受邀的特定同事(< 100 人) |
| 法律性質 | 公開傳輸/散布(高風險) | 私人分享(較低風險) |
| 實際被告機率 | 中 | 低 |
| 風險分級 | 🔴 高 | 🟡 中 |

**仍然存在的風險**:
- 重製權(把 PDF 存到 server)
- 公開傳輸權(透過網路提供下載)
- 但**可以主張**:
  - 服務性質是私人分享(非公開散布)
  - User 名單封閉(非公眾)
  - 有 admin approval gate(實質控制 access)
  - 有 audit log(可追蹤)
  - 有移除機制

### 7.2 免責聲明(About 頁)

```
本服務為 William Chang 私人架設,
僅供 William 本人與受邀使用者(同事)使用,作為學術與個人學習用途。

所有試卷來源為 StudyArk(全國中小學題庫網 - 學習方舟),
試卷版權歸原作者及各校所有。

如有版權方要求下架,請聯絡:
- Email: ...
- 移除保證:24 小時內處理

不對外公開、不收費、無廣告。
```

### 7.3 隱私
- ✅ 不收集個資(只存 Google 給的 email + name + picture URL)
- ✅ 不追蹤 user 行為做廣告
- ✅ IP 只存 SHA256 prefix(去識別化)
- ✅ 不分享給第三方

完整威脅模型見 [`docs/security-model.md`](docs/security-model.md)。

---

## 8. 開發里程碑

### Phase 0: 設計(現在)
- [x] 寫 PROJECT_PLAN.md(此文件,3rd revision)
- [x] 寫 README.md
- [x] 寫 docs/architecture.md / deployment.md / cookie-maintenance.md
- [x] 寫 docs/google-oauth-setup.md / user-management.md / security-model.md
- [x] 建 GitHub repo + push design docs
- [ ] **William review → 進 Phase 1**

### Phase 1: MVP(預計 2-3 週)
- [ ] Backend skeleton: FastAPI + SQLAlchemy + Pydantic
- [ ] Auth: Google OAuth + JWT + middleware
- [ ] Frontend: landing + login + search + admin
- [ ] Playwright scraper:從既有 script 移植
- [ ] SQLite schema + migration
- [ ] Caddyfile + systemd unit
- [ ] 本機跑得起來

### Phase 2: 上線(預計 3-5 天)
- [ ] Google Cloud Console 設定 OAuth client
- [ ] DDNS + Router port forwarding(William 已設定)
- [ ] Caddy + cert
- [ ] FastAPI 上 systemd
- [ ] 第一個 admin 登入測試
- [ ] 邀請第一批 5 個同事 user testing

### Phase 3: 觀察期(1-2 週)
- [ ] 監控流量 + cookies 狀態
- [ ] 看 StudyArk 有沒有 ban 跡象
- [ ] 收集同事 feedback
- [ ] Rate limit 調校
- [ ] Audit log review

### Phase 4: 加分題(視情況)
- [ ] 我的最愛學校
- [ ] 批次下載
- [ ] Welcome email
- [ ] i18n

---

## 9. 環境與部署決策(2026-07-18 拍板)

| 項目 | 決定 |
|------|------|
| **Hosting** | 家裡主機 GreenHouseUbuntu(已有 OpenClaw) |
| **DDNS** | `just4fun.myiphost.com` → `1.162.10.217` |
| **對外 port** | **8443**(HTTPS,William 已設 port forwarding) |
| **Reverse proxy** | Caddy 2.7(自動 HTTPS) |
| **Web server** | FastAPI 直接(uvicorn 2 workers) |
| **Process manager** | systemd(`tqark-web.service`) |
| **Google OAuth** | Web application,redirect to `:8443/auth/google/callback` |
| **Admin 設定** | 環境變數 `ADMIN_EMAILS=william@example.com`(可多個,逗號分隔) |
| **User 註冊** | 半開放(任何 Google 帳號可申請,需 admin approve) |
| **預期使用者數** | 50+ |

---

## 10. 已決策(原「開放問題」已全數拍板)

| 原 Q | 拍板結果 |
|------|---------|
| Q1. Hosting | ✅ 家裡主機 + DDNS + Caddy |
| Q2. 公開 vs 邀請制 | ✅ **半開放 + Admin approve** |
| Q3. UI 範圍 | ✅ 最小可用 + admin 必要功能 |
| Q4. Tech stack | ✅ Python + FastAPI + Playwright + SQLite + Caddy |
| Q5. Repo 命名 | ✅ `TQark-web` |

---

## 11. Coding / Debug 分工(2026-07-18 拍板)

- ✅ **Coding、debug、commit、push → 我(OpenClaw agent)來**
- ✅ **設計決策、技術選型 → 我建議,William 拍板**
- ✅ **執行 / 跑服務 / 抓 bug 結果 → William 回報**
- ✅ **William 負責的**:登 GitHub 加 SSH key、建 repo、登 Google OAuth 設定、跑 services、user testing

---

## 12. 待辦(設計確認後)

這個 design doc 過了之後,實際 coding 才開始:
- [ ] Phase 1 MVP 開工
- [ ] 從既有 `studyark_downloader.py` 拆出可重用的 scraper class
- [ ] FastAPI 專案初始化
- [ ] Caddyfile + systemd unit(可直接 copy 從 docs/deployment.md)
- [ ] Google OAuth 流程(按 docs/google-oauth-setup.md 設)
- [ ] Frontend HTML templates(landing + login + search + admin)
- [ ] 本地起服務測試
- [ ] Caddy + systemd 上 production

## 13. Future Improvements (Backlog, 2026-07-21+)

### Archive / OCR Pipeline
- [ ] **直書中文標題 OCR** (William feedback 2026-07-21 21:46)
  - 目前 `tesseract --psm 6 chi_tra` 對**直書 + 注音 + 圖片型** PDF 完全失效
  - 影響 19 個 其他X/ PDF (含 `30114_伸仁國小`, `22924_興南國小` 等)
  - **解法 (William 建議)**:逐字切割 + 90度旋轉
    1. 偵測 title 區域 (right strip, 假設 county 在最右直列)
    2. 沿垂直方向找字元邊界 (黑度變化)
    3. 切割成單字 boxes
    4. 每個字 box 轉 90度變橫書
    5. 丟給 tesseract (橫書模式處理)
  - 預期把 county 識別率 82% → 95%+
  - **優先度**:低 (因為其他 81% 已經足夠歸類 county,剩 19 個用 其他X 不影響功能)
- [ ] **county 字典建表**:用 Wikipedia API 或教育部學校資料庫查 county-school 對照
  - 對其他X/ 內的 filename 短名自動查表補 county
  - 估計每天多 1000 個 API call,還在 quota 內
- [ ] **Web UI county filter**:讓 user 可以從 dropdown 選 county 看所有 PDF
  - school_stats.json 已經是 county-aware,純前端工作

### Multi-account 改進
- [ ] **StudyArk counter API** 研究:能不能用 HEAD request 預先 check counter?
  - 避免下載到一半被踢掉浪費 quota
- [ ] **Per-account daily limit 自動學習**:觀察每個帳號實際 quota,寫進 account_status.json
  - 預設保守 30/day,實際可能 50-100

---

**Review checklist**:
- [ ] 環境檢查(對外 IP、port forwarding、RAM 夠)✅
- [ ] Google OAuth 設定步驟看完
- [ ] Caddy 安裝步驟看完
- [ ] Admin / User SOP 看完
- [ ] Security model 看完
- [ ] 法律風險評估可接受
- [ ] Coding 分工確認

確認過了就可以開工 Phase 1 🚀