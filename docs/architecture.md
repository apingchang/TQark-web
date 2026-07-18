# 系統架構

> 詳細說明 TQark Web 的系統組成、資料流、技術選型理由。

---

## 📐 整體架構圖

```
                  ┌──────────────────────────┐
                  │                          │
   訪客瀏覽器 ──► │   Cloudflare Edge        │
   (Web/Mobile)   │   - CDN cache            │
                  │   - DDoS protection      │
                  │   - Tunnel ingress       │
                  │                          │
                  └────────────┬─────────────┘
                               │ HTTPS (443)
                               ▼
                  ┌──────────────────────────┐
                  │  家裡主機 / VPS          │
                  │  ┌────────────────────┐  │
                  │  │   Nginx            │  │
                  │  │   :443 → :8000     │  │
                  │  └─────────┬──────────┘  │
                  │            ▼             │
                  │  ┌────────────────────┐  │
                  │  │   FastAPI          │  │
                  │  │   (Python 3.12)    │  │
                  │  └────┬──────┬────┬──┘  │
                  │       │      │    │     │
                  │       ▼      ▼    ▼     │
                  │   ┌─────┐ ┌────┐ ┌────┐ │
                  │   │SQLite│ │PDF │ │Play│ │
                  │   │meta  │ │cache│ │wri│ │
                  │   │ .db  │ │ /dir│ │ght│ │
                  │   └─────┘ └────┘ └─┬──┘ │
                  │                     │    │
                  └─────────────────────┼────┘
                                        │ HTTPS
                                        ▼
                              ┌──────────────────┐
                              │   StudyArk       │
                              │ studyark.org     │
                              │ (Google OAuth)   │
                              └──────────────────┘
```

---

## 🔄 關鍵流程

### 流程 A: 使用者搜尋試卷

```
使用者瀏覽器
  │
  │ GET /api/search?grade=8&year=113&semester=2&subject=國文&version=翰林
  ▼
FastAPI 接收請求
  │
  │ 1. 驗證參數 + rate limit 檢查
  ▼
MetadataCache (SQLite)
  │
  │ 2. 用篩選條件 hash 當 cache key 查詢
  │
  ├─ Cache HIT → 直接回 JSON
  │
  └─ Cache MISS → 進 step 3
       │
       │ 3. 啟動 Playwright,載入 StudyArk cookies
       │ 4. 開啟 https://www.studyark.org/exam-search
       │ 5. 套用篩選條件
       │ 6. parse HTML → 試卷列表
       │ 7. 寫進 SQLite cache(TTL 1 小時)
       ▼
     回 JSON 給瀏覽器
       │
       ▼
     前端 render 結果列表
```

### 流程 B: 使用者下載 PDF

```
使用者瀏覽器
  │
  │ GET /api/download/{exam_id}
  ▼
FastAPI 接收請求
  │
  │ 1. Rate limit 檢查
  ▼
PDFCache (本地檔案系統)
  │
  │ 2. 檢查 ./cache/pdfs/{exam_id}.pdf 是否存在
  │
  ├─ HIT → FileResponse(200, application/pdf)
  │
  └─ MISS → 進 step 3
       │
       │ 3. 啟動 Playwright
       │ 4. 用 exam_id 找到對應 StudyArk URL
       │ 5. 點下載按鈕,等 PDF 下載完
       │ 6. 存到 ./cache/pdfs/{exam_id}.pdf
       │ 7. Content-Disposition 設定檔名(套用 William 格式)
       ▼
     FileResponse
```

---

## 🗄️ 資料模型

### SQLite Schema (Metadata Cache)

```sql
CREATE TABLE IF NOT EXISTS search_cache (
    cache_key TEXT PRIMARY KEY,        -- SHA256(grade|year|semester|subject|version)
    query_params TEXT NOT NULL,        -- JSON: 原始篩選條件
    results_json TEXT NOT NULL,        -- JSON: 試卷列表
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP NOT NULL,     -- created_at + 1 hour
    hit_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS exam_metadata (
    exam_id TEXT PRIMARY KEY,          -- StudyArk 內部 ID
    school_name TEXT NOT NULL,
    grade INTEGER NOT NULL,
    year INTEGER NOT NULL,             -- 民國年
    semester INTEGER NOT NULL,         -- 1=上, 2=下
    domain TEXT,                       -- 領域: 語文領域/數學領域...
    subject TEXT NOT NULL,
    exam_number INTEGER,               -- 第幾次段考
    exam_type TEXT,                    -- 期中考/期末考
    version TEXT,                      -- 翰林/康軒/南一
    has_answer BOOLEAN DEFAULT FALSE,
    studyark_url TEXT NOT NULL,
    last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS download_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exam_id TEXT NOT NULL,
    downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    source TEXT,                       -- 'cache' or 'studyark'
    ip_hash TEXT                       -- 雜湊過的 IP(不存原始)
);

CREATE INDEX IF NOT EXISTS idx_search_expires ON search_cache(expires_at);
CREATE INDEX IF NOT EXISTS idx_exam_lookup ON exam_metadata(grade, year, semester, subject, version);
CREATE INDEX IF NOT EXISTS idx_download_log_time ON download_log(downloaded_at);
```

---

## 🛠️ 技術選型理由

### 為什麼 Python + FastAPI?
- William 既有 `studyark_downloader.py` 是 Python,直接搬 function
- FastAPI 原生 async,跟 Playwright async API 完美搭配
- type hints + 自動 OpenAPI 文件
- 啟動快、記憶體小

### 為什麼純 HTML + Vanilla JS(不用 React/Vue)?
- 整個 app 就 3 個頁面
- Build step 對 1 個小 app 是純粹負擔
- Tailwind CDN → 樣式漂亮但不用設定 webpack
- 之後需要再升級不難(Next.js / Nuxt)

### 為什麼 SQLite?
- 預估流量 < 100 req/day,SQLite 完全勝任
- 不用額外 process / port
- 本地檔案直接備份
- 之後流量大可以無痛升 Postgres

### 為什麼本地檔案 PDF cache(不用 S3)?
- MVP 階段省事
- 家裡硬碟夠大(都已經有外接 My Book)
- 之後量大再搬到 Cloudflare R2(便宜,S3-compatible)

### 為什麼 Cloudflare Tunnel(不用直接 port forward)?
- 免費 HTTPS(Let's Encrypt 都不用設定)
- 隱藏家裡真實 IP(防 StudyArk ban IP)
- 內建基本 DDoS 防護
- 跟 William 既有 Tailscale / Cloudflare 工具鍊一致

---

## 📊 預估流量模型

| 指標 | 預估 |
|------|------|
| 日活躍使用者 | 10-50 人 |
| 單日搜尋請求 | 50-200 次 |
| 單日下載請求 | 20-100 次(平均每搜尋 0.5 下載) |
| 平均 PDF 大小 | 1-3 MB |
| 單日流量 | 50-300 MB(下載) + 5-20 MB(其他) |
| StudyArk 請求節省(因 cache) | 預估 60-80% |

---

## 🔐 安全性設計

1. **環境隔離**
   - cookies 檔案 `.gitignore`(絕不入 repo)
   - `.env` 也不入 repo
2. **API 安全**
   - Rate limit:10 req/min per IP
   - Cloudflare Turnstile(之後,可選)
   - 不暴露內部 admin endpoint
3. **輸入驗證**
   - Pydantic schema 驗證所有 query params
   - path traversal 防護(只用 exam_id,不直接吃 filename)
4. **資源限制**
   - 下載 timeout(預設 60 秒)
   - PDF 大小上限(預設 20 MB)
5. **日誌**
   - 不 log 完整 IP(只 log SHA256 prefix)
   - 不 log cookies 內容

---

## 📈 監控指標

健康檢查 `/health`:
- FastAPI 進程 alive
- SQLite 可寫
- `./cache/` 資料夾可寫
- cookies 檔案存在 + 未過期(< 30 天)
- StudyArk 可達(每 10 分鐘 health check 一次)

之後會接:
- Cloudflare Analytics(流量)
- UptimeRobot 或類似(可用性)
- Sentry 或類似(錯誤追蹤,選擇性)