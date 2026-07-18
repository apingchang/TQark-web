# 系統架構

> 詳細說明 TQark Web 的系統組成、資料流、技術選型理由、DB schema。

---

## 📐 整體架構圖

```
                  ┌──────────────────────────┐
                  │                          │
   User/Admin  ──►│   Browser (User)         │
   (Web UI)      │                          │
                  └────────────┬─────────────┘
                               │ HTTPS (8443)
                               ▼
                  ┌──────────────────────────┐
                  │  家裡 Router             │
                  │  (Port Forwarding)       │
                  └────────────┬─────────────┘
                               │ LAN
                               ▼
                  ┌──────────────────────────┐
                  │  GreenHouseUbuntu Linux  │
                  │  ┌────────────────────┐  │
                  │  │  Caddy :8443       │  │
                  │  │  - HTTPS (Let's    │  │
                  │  │    Encrypt auto)   │  │
                  │  │  - Security headers│  │
                  │  └─────────┬──────────┘  │
                  │            ▼             │
                  │  ┌────────────────────┐  │
                  │  │  FastAPI :8000     │  │
                  │  │  (127.0.0.1 only)  │  │
                  │  │                    │  │
                  │  │  /auth/*           │  │
                  │  │  /api/search       │  │
                  │  │  /api/download     │  │
                  │  │  /admin/*          │  │
                  │  │  /health           │  │
                  │  └──┬─────┬──────┬───┘  │
                  │     │     │      │      │
                  │     ▼     ▼      ▼      │
                  │  ┌─────┐ ┌────┐ ┌────┐ │
                  │  │SQLite│ │PDF │ │Play│ │
                  │  │ DB  │ │cache│ │wri│ │
                  │  │     │ │ /dir│ │ght│ │
                  │  └─────┘ └────┘ └──┬─┘ │
                  └────────────────────┼────┘
                                       │ HTTPS
                                       ▼
                              ┌──────────────────┐
                              │   StudyArk       │
                              │ studyark.org     │
                              │ (Google OAuth)   │
                              └──────────────────┘

                  ┌──────────────────────────┐
                  │  Google OAuth            │
                  │  accounts.google.com     │
                  └──────────────────────────┘
```

---

## 🔄 關鍵流程

### 流程 A:User 申請 + Admin Approve

```
1. User 開瀏覽器到 https://just4fun.myiphost.com:8443/
2. 看到 landing page,點「Sign in with Google」
3. 跳到 Google OAuth,選帳號,同意授權
4. Google callback 到 /auth/google/callback?code=xxx
5. FastAPI 用 code 換 access_token
6. 從 Google API 拿 user info (email, name, picture, google_id)
7. 查 users table:
   - 不存在 → INSERT, status='pending', role='user'
     (但 email 在 ADMIN_EMAILS env → role='admin')
   - 存在 → UPDATE last_login_at
8. 設 JWT cookie (httpOnly, SameSite=Strict, 24h expire)
9. 重導到 /dashboard
   - User status=pending → 看到「等待中」頁面
   - User status=approved → 看到搜尋頁面
   - User status=blocked → 看到「帳號被停用」

10. User 填「為什麼想加入」訊息 → POST /api/access-requests
11. 寫進 users.application_message

12. Admin 登入 → /admin/users?status=pending
13. 看到 User 申請 → 看訊息 → 點 Approve
14. UPDATE users SET status='approved', approved_at=now(), approved_by=admin.id
15. 寫 audit_log (action='user_approve', user_id=applicant.id)
16. User 下次登入 → 看到搜尋介面
```

### 流程 B:搜尋試卷

```
User (approved) → GET /api/search?grade=8&year=113&semester=2&subject=國文&version=翰林

1. Auth middleware 驗 JWT cookie → 確認 user.status='approved'
2. Rate limit check (slowapi, 10 req/min per user for search)
3. 計算 cache_key = SHA256("8|113|2|國文|翰林")
4. 查 search_cache table:
   - hit (且沒過期) → increment hit_count → 回 JSON
   - miss 或過期 → 進 step 5
5. 啟動 Playwright,載入 StudyArk cookies
6. 開啟 https://www.studyark.org/exam-search
7. 套用篩選條件,wait for 結果列表
8. Parse HTML → exam_metadata list
9. 寫進 search_cache (TTL 1 小時)
10. 寫 audit_log (action='search', target=query_params, user_id)
11. 回 JSON 給前端
```

### 流程 C:下載 PDF

```
User → GET /api/download/{exam_id}

1. Auth middleware 驗 JWT → 確認 user.status='approved'
2. Rate limit check (5 req/min per user, 30 req/hour per user)
3. 查 pdf_cache table:
   - hit → FileResponse (更新 last_accessed_at) → 寫 audit_log
   - miss → 進 step 4
4. 查 exam_metadata 拿 studyark_url
5. Playwright 開 StudyArk 頁面,點下載,等 PDF 檔
6. 存到 ./data/pdfs/{exam_id}.pdf (chmod 644)
7. INSERT pdf_cache (exam_id, file_path, size_bytes)
8. FileResponse + Content-Disposition (套用 William 檔名格式)
9. 寫 audit_log (action='download', target=exam_id, metadata={school, subject})
```

### 流程 D:Admin Block User

```
Admin → POST /admin/users/{id}/block

1. Auth middleware 驗 JWT → 確認 user.role='admin'
2. 確認 target user.id != self.id(不能 block 自己)
3. UPDATE users SET status='blocked', blocked_at=now(), blocked_reason=?
4. 寫 audit_log (action='user_block', user_id=target.id, metadata={by_admin, reason})
5. 回 JSON { success: true }
6. Target user 下次登入 → 看到「帳號被停用」
```

---

## 🗄️ DB Schema

詳細 schema 見 [`docs/user-management.md`](docs/user-management.md) 第「技術細節」段。

**核心 table 摘要**:

| Table | 用途 | 關鍵欄位 |
|-------|------|---------|
| `users` | User/Admin 資料 | email, name, role, status, google_id |
| `search_cache` | 搜尋結果 cache | cache_key, results_json, expires_at |
| `exam_metadata` | 試卷 metadata | exam_id, school, grade, year, subject, version |
| `pdf_cache` | PDF 檔案索引 | exam_id, file_path, size_bytes |
| `audit_log` | 所有操作記錄 | user_id, action, target, ip_hash |

---

## 🛠️ 技術選型理由

### 為什麼 Python + FastAPI?
- William 既有 `studyark_downloader.py` 是 Python,直接搬 function
- FastAPI 原生 async,跟 Playwright async API 完美搭配
- type hints + 自動 OpenAPI 文件
- 啟動快、記憶體小

### 為什麼純 HTML + Vanilla JS(不用 React/Vue)?
- 整個 app 就 3-5 個頁面(landing, login, search, admin)
- Build step 對 1 個小 app 是純粹負擔
- Tailwind CDN → 樣式漂亮但不用設定 webpack

### 為什麼 SQLite?
- 預估 50+ user,< 100 req/day,SQLite 完全勝任
- 不用額外 process / port
- 本地檔案直接備份
- 之後流量大可以無痛升 Postgres

### 為什麼 Caddy(不選 Nginx)?
- **自動 HTTPS**(Let's Encrypt cert 自動 renew)
- Caddyfile 3 行搞定 reverse proxy
- 預設安全 headers(只要寫進 config)
- 對 DDNS 友善
- 比 Nginx 簡單 5 倍

### 為什麼 Google OAuth(不自己管密碼)?
- 不用存密碼 → 沒被盜風險
- Google 幫你做 2FA
- 一鍵登入 UX 好
- 免費

### 為什麼 JWT + httpOnly cookie(不 localStorage)?
- httpOnly cookie → JS 讀不到 → 防 XSS 偷 token
- SameSite=Strict → 防 CSRF
- 24h expire → 被偷也只能用一天
- 之後可以加 refresh token

### 為什麼不 Docker?
- 直接 Python + systemd 比較簡單
- 跟 OpenClaw 部署方式一致
- 少一層抽象,debug 容易

---

## 📊 預估流量模型

| 指標 | 預估 |
|------|------|
| User 總數 | 50+ |
| Daily active users | 5-15 |
| 單日搜尋請求 | 30-100 |
| 單日下載請求 | 20-80 |
| 平均 PDF 大小 | 1-3 MB |
| 單日下載流量 | 20-240 MB |
| StudyArk 請求節省(因 cache) | 預估 60-80% |

---

## 🔐 安全性設計

完整見 [`docs/security-model.md`](docs/security-model.md)。

### API 安全
- JWT middleware 驗所有 `/api/*` 跟 `/admin/*`
- 不同 endpoint 不同 rate limit
- SlowAPI 實作
- 不暴露內部 admin API(都要過 JWT + role check)

### 輸入驗證
- 所有 query params 用 Pydantic schema 驗證
- path 用 regex(只 alphanumeric + dash)
- SQLAlchemy ORM(避免 raw SQL)
- Jinja2 auto-escape

### Resource limits
- Download timeout 60s
- PDF 大小上限 20 MB
- Rate limit 防止資源耗盡

### 日誌
- Audit log 不可變(只 INSERT,不 UPDATE/DELETE)
- IP 只存 hash(SHA256 prefix 16 chars)
- 不 log cookies / JWT token / 密碼

---

## 📈 監控指標

`/health` endpoint 回傳:
```json
{
  "status": "ok",
  "db": "ok",
  "pdf_cache_writable": true,
  "cookies_valid": true,
  "cookies_expires_at": "2026-09-15T10:00:00",
  "studyark_reachable": true,
  "last_check_at": "2026-07-18T18:00:00"
}
```

之後可接:
- Caddy log 監控(失敗率、慢回應)
- Audit log 異常 alert
- Disk usage alert(PDF cache 塞爆)