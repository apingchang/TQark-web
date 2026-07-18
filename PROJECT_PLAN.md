# TQark Web — 專案設計書

> 將現有的 StudyArk 考古題收集流程,封裝成一個對外提供服務的網頁應用。
> 訪客透過瀏覽器篩選條件 → 看到所有符合的試卷 → 點擊下載 PDF。

**狀態**: 設計階段(尚未開工 coding)
**作者**: William Chang
**協作者**: 夥計 (OpenClaw main agent)
**日期**: 2026-07-18

---

## 1. 目標

### 1.1 解決的問題
目前 William 抓考古題是「手動 + Playwright script + 本地資料夾」流程,
只能自己用。如果要分享給其他老師、家長、學生使用,沒有友善介面。

### 1.2 目標使用者
- **主要**: 家裡有國中生的家長(自家社群 → 之後可拓展)
- **次要**: 國中國文/數學等科老師、補習班、學生自習
- **不會是**: 一般民眾、營利單位(規模/法律考量)

### 1.3 核心價值
| 對誰 | 解決什麼 |
|------|---------|
| 家長 | 「我家孩子八年級國文翰林第二段考考古題哪裡找」 → 5 秒內拿到 PDF |
| 老師 | 「我要看台北市某國中 113 上學期自然科第一次段考」 → 直接下載 |
| 學生 | 「考前衝刺考古題,順便看看其他學校怎麼出」 |

---

## 2. 功能範圍(MVP)

### 2.1 一定要有(P0)
- [ ] 搜尋表單:年級 / 學年度 / 學期 / 領域 / 科目 / 版本
- [ ] 結果列表:學校名 + 試卷名 + 下載按鈕(有答案卷就兩個按鈕)
- [ ] 下載:點下去直接拿到 PDF,檔名自動套用 William 既有格式
- [ ] 健康檢查頁 `/health`(給 monitoring 用)
- [ ] 一個簡單 About 頁(說明來源、免責、聯絡)

### 2.2 應該要有(P1)
- [ ] Rate limit(每人 10 req/min,防止被刷流量)
- [ ] PDF cache:同一份試卷只從 StudyArk 拉一次,後續直接從本地送
- [ ] Cookie 過期監控 + alert(cookies 失效時通知 William)
- [ ] 簡單的搜尋結果快取(metadata level,降低 StudyArk 請求量)

### 2.3 可以之後再加(P2)
- [ ] 「我的最愛學校」(讓使用者存常用學校,免重新選)
- [ ] 批次下載(勾選多份 → 一次打包 zip)
- [ ] 「熱門下載 Top 10」列表
- [ ] i18n 雙語切換(中/英)
- [ ] 訂閱電子報(有新試卷通知)
- [ ] 統計儀表板(對外公開的公益感)
- [ ] Discord/Telegram bot 介面

### 2.4 不要做
- 帳號系統(登入/註冊/密碼)— 純公開服務省麻煩
- 評論/評分 — 沒必要
- 內容審核 queue — StudyArk 上有問題的題目不歸我們管
- 上傳功能 — 只下載不上傳

---

## 3. 技術選型

### 3.1 Backend: Python 3.12 + FastAPI
**為什麼選 Python**:
- 跟 William 既有 `studyark_downloader.py` script 語言一致
- Playwright 在 Python 跟 Node 都行,但 Python 對資料處理、檔案管理更順
- async/await 友善

**為什麼選 FastAPI 而非 Flask/Django**:
- 原生 async(Playwright + httpx 都是 async 友善)
- 自動 OpenAPI 文件(開發時超好用)
- type hints + Pydantic 驗證(降低 bug)
- 輕量,沒有 Django 那麼多預設包袱

### 3.2 Frontend: 純 HTML + Vanilla JS + Tailwind CDN
**為什麼不用 React/Vue**:
- 這是個小 app,3 個頁面就夠,build step 完全是負擔
- 純 HTML 一個檔就能跑,部署超簡單
- Tailwind CDN 讓樣式好看但不用設定 webpack

**頁面結構**:
- `index.html` — 搜尋首頁(主要)
- `about.html` — 說明/免責
- 之後可能加 `stats.html`(公開統計)

### 3.3 Cache: SQLite + 本地檔案
**為什麼 SQLite 而非 Redis**:
- 流量低(預估 < 100 req/day),SQLite 綽綽有餘
- 不用額外起一個 process
- 本地檔案直接備份

**為什麼本地檔案而非 S3**:
- MVP 階段本地 `./cache/pdfs/` 就好
- 之後流量大再考慮 Cloudflare R2(便宜、S3-compatible)

### 3.4 反向代理 + HTTPS: Nginx + Cloudflare Tunnel
**為什麼 Cloudflare Tunnel**:
- **免費**對外(不用花 VPS 月費)
- **免費** HTTPS(Let's Encrypt 都不用設)
- 隱藏家裡的真實 IP
- William 已熟悉 Tailscale,Cloudflare Tunnel 概念一樣(zero-trust tunnel)

**為什麼不用直接 port forwarding**:
- 家裡 IP 可能被 StudyArk 黑名單
- Cloudflare 提供基本 DDoS 防護
- 之後想換網域超方便

### 3.5 Deploy: Docker Compose
- `docker-compose.yml` 一鍵起 backend + nginx
- 本機開發跟 Cloudflare Tunnel 部署都用同一份設定
- 之後想搬到 VPS,Hetzner CX22(€4/月)之類的也行

---

## 4. 系統架構

```
                  ┌──────────────────────┐
   訪客瀏覽器  ──► │  Cloudflare Edge    │
                  │  (CDN + Tunnel)      │
                  └──────────┬───────────┘
                             │ HTTPS
                             ▼
                  ┌──────────────────────┐
                  │  家裡主機 / VPS      │
                  │  ┌────────────────┐  │
                  │  │ Nginx          │  │
                  │  │ :443 → :8000   │  │
                  │  └────────┬───────┘  │
                  │           ▼          │
                  │  ┌────────────────┐  │
                  │  │ FastAPI        │  │
                  │  │ (Python)       │  │
                  │  └──┬─────┬────┬──┘  │
                  │     │     │    │     │
                  │     ▼     ▼    ▼     │
                  │  SQLite  PDF  Play-  │
                  │  meta-   cache wright │
                  │  data    /dir  /     │
                  │  .db            Study │
                  │                Ark   │
                  └──────────────────────┘
```

### 4.1 關鍵流程
**搜尋**:
```
GET /api/search?grade=8&year=113&semester=2&subject=國文&version=翰林
  → FastAPI 查 SQLite metadata cache
  → cache hit → 直接回 JSON 列表
  → cache miss → Playwright 開 StudyArk、篩選、parse 列表、回 JSON + 寫 cache
```

**下載**:
```
GET /api/download/<exam_id>
  → 查 cache,看 PDF 是否已下載
  → hit → 直接 FileResponse
  → miss → Playwright 開 StudyArk、下載 PDF、寫 cache、FileResponse
```

### 4.2 StudyArk 互動(共用單一 cookies)
- server 端存一份 `/data/cookies/studyark_cookies.json`
- 開頭需要 William 手動 Playwright 登入 Google → 存 cookies
- 之後所有請求都用這份 cookies
- **風險**:StudyArk 看到單一 IP 大量流量可能 ban
- **緩解**:
  - Rate limit(每人 10 req/min)
  - 預先 cache 熱門試卷(降低 80% StudyArk 請求)
  - 監控 + alert(流量異常時 email/Discord 通知)

---

## 5. 專案結構(預定)

```
TQark-web/
├── README.md                          # 使用說明(中英雙語)
├── LICENSE                            # MIT
├── CHANGELOG.md                       # 版本紀錄
├── CONTRIBUTING.md                    # 貢獻指南
├── .gitignore
├── .dockerignore
├── docs/
│   ├── architecture.md                # 詳細架構、流量模型
│   ├── deployment.md                  # 部屬指南(Docker / bare-metal / Cloudflare Tunnel)
│   ├── cookie-maintenance.md          # cookies 怎麼維護、過期怎麼辦
│   └── screenshots/                   # UI 截圖(等做出來再放)
├── backend/
│   ├── pyproject.toml                 # Python 套件設定
│   ├── requirements.txt
│   ├── .env.example
│   ├── Dockerfile
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py                    # FastAPI app entry
│   │   ├── config.py                  # 設定(env vars)
│   │   ├── routes/
│   │   │   ├── __init__.py
│   │   │   ├── search.py              # /api/search
│   │   │   ├── download.py            # /api/download/{id}
│   │   │   └── health.py              # /health
│   │   ├── services/
│   │   │   ├── studyark_scraper.py    # Playwright + StudyArk
│   │   │   ├── pdf_cache.py           # 本地 PDF cache
│   │   │   ├── metadata_cache.py      # SQLite metadata cache
│   │   │   └── rate_limiter.py        # slowapi
│   │   ├── models/
│   │   │   └── schemas.py             # Pydantic
│   │   └── utils/
│   │       └── naming.py              # 檔名格式化
│   ├── scripts/
│   │   ├── refresh_cookies.py         # 手動重抓 cookies
│   │   ├── seed_cache.py              # 預先 cache 熱門試卷
│   │   └── check_health.py            # 定期健康檢查(可上 cron)
│   └── tests/
│       ├── test_naming.py
│       ├── test_cache.py
│       └── fixtures/
├── frontend/
│   ├── index.html                     # 搜尋主頁
│   ├── about.html                     # 說明/免責
│   ├── assets/
│   │   ├── app.js
│   │   ├── styles.css
│   │   └── logo.svg
│   └── nginx.conf                     # 給靜態檔案 hosting 用
├── deploy/
│   ├── docker-compose.yml             # 一鍵起服務
│   ├── nginx/
│   │   ├── nginx.conf
│   │   └── conf.d/
│   │       └── studyark.conf
│   └── cloudflare/
│       └── README.md                  # Cloudflare Tunnel 設定指南
└── .github/
    └── workflows/
        └── ci.yml                     # lint + test on PR
```

---

## 6. 命名規則

### 6.1 Repo
- GitHub: `apingchang/TQark-web`
- 顯示名稱: **TQark Web**(英) / **國中考古題下載站**(中)

### 6.2 下載檔名(沿用 William 既有格式)
```
{學校名} {年級} {學年度} {學期} {領域} {科目} {第幾次段考} {期中考/期末考} {版本} 試卷.pdf
{學校名} {年級} {學年度} {學期} {領域} {科目} {第幾次段考} {期中考/期末考} {版本} 試卷_答案卷.pdf
```
例:`市立同德國中 八年級 113 下學期 語文領域 國文 第二次段考 期中考 翰林 試卷.pdf`

---

## 7. 安全 / 法律 / 隱私

### 7.1 法律
- **免責聲明**(about 頁必寫):
  - 本站所有試卷來源為「StudyArk 全國中小學題庫網」
  - 試卷版權歸原作者/學校所有
  - **僅供學術與個人學習使用**,請勿用於營利或侵權用途
  - 如有版權問題,聯絡下架

### 7.2 隱私
- **不收集個資**:不用帳號、不用 cookie 追蹤(僅 Cloudflare 必要 analytics)
- **不存訪客 IP**(或僅短暫留存用於 rate limit)
- **Cloudflare 隱私政策**會在 about 頁標明

### 7.3 服務本身的安全
- 不對外暴露內部 admin API(健康檢查除外)
- Rate limit 防濫用
- Cloudflare Tunnel 隱藏家裡 IP

---

## 8. 開發里程碑

### Phase 0: 設計(現在)
- [x] 寫 PROJECT_PLAN.md
- [ ] 建 GitHub repo(空 repo + scaffold 推上去)
- [ ] William review 通過 → 進 Phase 1

### Phase 1: MVP(預計 1-2 週)
- [ ] Backend: FastAPI 骨架 + `/api/search` + `/api/download/{id}` + `/health`
- [ ] Playwright scraper(從既有 script 移植)
- [ ] Frontend: 搜尋表單 + 結果列表(超簡潔版)
- [ ] 本機 docker-compose 跑得起來

### Phase 2: 對外(預計 3-5 天)
- [ ] Cloudflare Tunnel 設定
- [ ] Rate limit middleware
- [ ] PDF cache(避免重複抓)
- [ ] Metadata cache(SQLite)
- [ ] About 頁 + 免責聲明

### Phase 3: 觀察期(上線後 1-2 週)
- [ ] 監控流量 + cookies 狀態
- [ ] 看 StudyArk 有沒有 ban 跡象
- [ ] 收集使用者回饋(放 Google Form 在 about 頁)
- [ ] 決定要不要加 P2 功能

### Phase 4: 加分題(視情況)
- [ ] 批次下載
- [ ] 我的最愛學校
- [ ] i18n
- [ ] Discord bot

---

## 9. 開放問題(給 William 決定)

### Q1: Hosting 在哪?
| 選項 | 優點 | 缺點 |
|------|------|------|
| **A. 家裡+Cloudflare Tunnel** | 免費、隱藏 IP、DDoS 防護 | 跟家裡網路綁一起 |
| **B. 雲端 VPS**(Hetzner €4/月) | 獨立服務、可隨時搬家 | 要花錢、自己管 |
| C. 家裡直接 port forward | 最簡單 | 暴露 IP、StudyArk 可能 ban |

**建議**: A,跟 William 既有 OpenClaw 部署同一台(已在跑服務),Cloudflare Tunnel 工具他已熟悉。

### Q2: 公開 vs 邀請制?
| 選項 | 優點 | 缺點 |
|------|------|------|
| **A. 完全公開 + rate limit** | 推廣容易、社群分享友善 | 怕被刷流量 |
| B. 邀請制(token 連結) | 流量可控 | 要管理邀請名單 |
| C. 帳號系統 | 完全控制 | 開發+維護成本高 |

**建議**: A,搭配 10 req/min rate limit + Cloudflare Turnstile(防機器人)。

### Q3: 第一版 UI 範圍?
- A. **最小可用**(搜尋表單 + 結果 + 下載)— 推薦,1-2 週完工
- B. 包含 P1 所有功能 — 3-4 週
- C. 包含 P2 — 1 個月+

### Q4: Tech Stack 同意嗎?
- Backend: Python + FastAPI + Playwright ✅
- Frontend: 純 HTML + Tailwind CDN ✅
- Cache: SQLite + 本地檔案 ✅
- 部署: Docker Compose + Cloudflare Tunnel ✅
- 如果有其他偏好(例如偏好 Node.js 後端、React 前端),現在講我換

### Q5: Repo 命名?
- **`TQark-web`**(推薦,簡潔明瞭)
- `studyark-downloader`
- `studyark-public`
- 其他?

---

## 10. 待辦(設計確認後)

這個 design doc 過了之後,實際 coding 才開始:
- [ ] Phase 1 MVP 開工
- [ ] 從既有 `studyark_downloader.py` 拆出可重用的 scraper class
- [ ] FastAPI 專案初始化
- [ ] Docker Compose 本機起服務
- [ ] 前端 HTML 模板
- [ ] CI/CD pipeline

---

**Review checklist**:
- [ ] Q1-Q5 都有答案
- [ ] Project structure 合理
- [ ] Phase 1 範圍 OK
- [ ] 法律/隱私說明接受
- [ ] 沒漏掉的 requirement

確認過了就可以開工 🚀