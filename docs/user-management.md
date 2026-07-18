# User Management 指南

> Admin 跟 User 的 SOP,涵蓋申請、approve、block、audit log。

---

## 👥 角色定義

| 角色 | 權限 | 怎麼成為 |
|------|------|---------|
| **Visitor**(訪客) | 只能看到 landing page | 任何人(沒登入) |
| **User**(已登入) | 看 landing + 申請 access | Google 登入後 |
| **Pending User** | 帳號待審核,看不到搜尋介面 | Google 登入 + 申請 access |
| **Approved User** | 用搜尋 + 下載 | Admin approve 後 |
| **Blocked User** | 看到「帳號被停用」 | Admin block 或自己 revoke |
| **Admin** | 全部 User 權限 + 管理介面 | 第一個 admin 用 env var `ADMIN_EMAILS`,之後 admin 可以 promote 其他人 |

---

## 🚶 User 申請 Access 流程

### 首次造訪
1. User 開瀏覽器到 `https://just4fun.myiphost.com:8443/`
2. 看到 landing page:
   - 服務名稱
   - 「Sign in with Google」按鈕
   - 「關於」說明
3. 點 Google 登入 → 跳到 Google → 選帳號 → 同意授權
4. 回來看到「申請 access」頁面:
   - 自動填入 Google email + 姓名
   - 顯示「為什麼想加入?」文字框(optional)
   - 「送出申請」按鈕
5. 送出後看到「申請已送出,等待 admin 核准」訊息
6. **此時 user 還不能 access 任何東西**

### 等待審核
- User 重新整理頁面會一直看到「等待中」訊息
- 通常 1-3 天內會被處理
- Admin 處理完會寄信(Phase 1 簡化版:不寄信,User 自己回來看)

### Approved 後
- User 下次登入直接看到「歡迎,XXX」+ 搜尋介面
- 開始用搜尋 / 下載功能

### 被拒
- User 下次登入看到「申請被拒,聯絡 admin」
- 可以請 User 再申請一次,或 admin 改變心意

---

## 👑 Admin SOP

### 第一次設定(你自己)
1. 在 `.env` 設 `ADMIN_EMAILS=your.email@gmail.com`
2. 用你的 Google 帳號登入
3. 系統比對 email → 自動給 admin role
4. 進 `/admin` 看到 dashboard

### Admin Dashboard 功能

#### 1. 看 Pending User 列表
- 路徑: `/admin/users?status=pending`
- 顯示:姓名、email、申請時間、申請訊息
- 操作:**Approve** / **Reject**

#### 2. 看所有 User
- 路徑: `/admin/users`
- 顯示:所有 user + 狀態(approved / blocked / pending)+ 最後登入時間
- 操作:**Block** / **Unblock** / **Promote to Admin** / **Demote**

#### 3. 看 Audit Log
- 路徑: `/admin/audit`
- 顯示:每筆操作(誰、做了什麼、何時)
- 篩選:user / action / time range
- 匯出:CSV(給 audit 用)

#### 4. 看流量統計(可選)
- 路徑: `/admin/stats`
- 顯示:本週 / 本月 / 全部的搜尋次數、下載次數、不重複 user
- 圖表:Chart.js(輕量)

#### 5. 設定(可選)
- 路徑: `/admin/settings`
- 改服務名稱、公告、API rate limit

---

## 🛠 日常 Admin 操作

### Approve User
```
1. Admin 收到 User 申請(透過 email / 自己看 dashboard)
2. Admin 登入 → /admin/users?status=pending
3. 看到新申請 → 看 email + 訊息 → 確認是認識的人
4. 點 Approve
5. User 狀態變 approved
6. User 下次登入可以用
```

### Block User(例如同事離職)
```
1. Admin 登入 → /admin/users
2. 找到 user → 點 Block
3. 確認對話框 → 填原因(選填,寫進 audit log)
4. User 狀態變 blocked
5. User 下次登入看到「帳號被停用」
```

### Promote User 為 Admin(信任的同事幫忙管理)
```
1. Admin 登入 → /admin/users
2. 找到 user → 點 Promote to Admin
3. 確認對話框
4. User role 變 admin
5. 之後該 user 登入也能看到 /admin 選單
```

### 看誰抓了什麼
```
1. Admin 登入 → /admin/audit
2. 篩選 user 或 time range
3. 看每筆操作的:
   - user_name
   - action (search / download)
   - target (搜尋條件 / exam_id)
   - timestamp
   - ip_hash
```

---

## 🔐 Admin 安全規則

### 保護自己的 Admin 帳號
- ✅ 用 Google 帳號 2FA
- ✅ 不要在公用電腦登入
- ✅ 定期檢查 audit log(看有沒有可疑操作)

### Block 原則
- 同事離職 → **立即 Block**(不等)
- 同事帳號被盜 → **立即 Block + Google 重設密碼**
- 同事單純不合作 → 先警告,還是沒改善再 Block
- Block 原因一定要寫進 audit log

### Promote Admin 謹慎
- 只給你完全信任的同事
- 一旦是 admin,他們可以 approve / block 別人
- 建議不要 promote 超過 1-2 個副 admin

---

## 📊 Audit Log 詳解

### 記錄什麼
| Action | 記錄內容 |
|--------|---------|
| `user_login` | user_id, timestamp, ip_hash |
| `user_register` | user_id, email, timestamp, ip_hash |
| `user_approve` | user_id, approved_by (admin_id), timestamp |
| `user_reject` | user_id, rejected_by, timestamp, reason |
| `user_block` | user_id, blocked_by, timestamp, reason |
| `user_unblock` | user_id, unblocked_by, timestamp |
| `user_promote_admin` | user_id, promoted_by, timestamp |
| `search` | user_id, query_params (JSON), result_count, timestamp |
| `download` | user_id, exam_id, school_name, subject, timestamp |
| `download_denied` | user_id, reason, timestamp |
| `admin_action` | admin_id, action_type, target, timestamp |

### 不記錄什麼
- ❌ 完整 IP(`ip_hash` 是 SHA256 prefix 前 16 chars,不可逆但可去識別)
- ❌ Cookies 內容
- ❌ JWT token 內容
- ❌ 密碼(我們也沒存)

### Retention(保留多久)
- 預設 **永久**(備份 DB 就有 audit log)
- 之後可以加 auto-archive 超過 1 年的 log

---

## 🚨 緊急情況 SOP

### 我被盜帳號怎麼辦?
1. **立即**到 Google 改密碼 + 撤銷所有 sessions
2. 登入 TQark-web(用別的方式)— 如果連不到,SSH 進 server
3. 在 SQLite 直接把自己的 user block:
   ```bash
   sqlite3 /home/aping/TQark-web/data/db/app.db \
     "UPDATE users SET status='blocked' WHERE email='your.email@gmail.com';"
   ```
4. 等 Google 帳號拿回來,再請另一個 admin unblock

### Admin 帳號全沒了怎麼辦?
1. SSH 進 server
2. 編輯 `.env`,把其他 email 加進 `ADMIN_EMAILS`
3. 重啟服務
4. 該 user 下次登入自動變 admin

### DB 壞了怎麼辦?
1. 從最近的 backup 還原(記得設 backup cron!)
2. 沒 backup 的話...只能請所有 user 重新申請

---

## 📋 Checklist

### Phase 1 上線前
- [ ] `.env` 的 `ADMIN_EMAILS` 設好你的 email
- [ ] 用你 Google 帳號登入 → 確認能進 `/admin`
- [ ] 看 Pending User 列表是空的
- [ ] 看 Audit Log 是空的(或只有你的登入)

### 之後日常
- [ ] 收到申請 → 1-3 天內處理
- [ ] 同事離職 → 立即 block
- [ ] 每月看一次 audit log(找可疑操作)
- [ ] 每季 backup DB

---

## 🔧 技術細節(給之後 debug 用)

### DB Schema

```sql
-- Users table
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    picture_url TEXT,
    role TEXT NOT NULL DEFAULT 'user',  -- 'user' | 'admin'
    status TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'approved' | 'blocked'
    google_id TEXT UNIQUE NOT NULL,
    approved_at TIMESTAMP,
    approved_by INTEGER,  -- user_id of admin
    last_login_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (approved_by) REFERENCES users(id)
);

-- Audit log
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    action TEXT NOT NULL,  -- 'user_login' | 'search' | 'download' | ...
    target TEXT,           -- exam_id, search params, etc
    metadata_json TEXT,    -- extra context
    ip_hash TEXT,          -- SHA256(ip)[:16]
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE INDEX idx_users_status ON users(status);
CREATE INDEX idx_users_email ON users(email);
CREATE INDEX idx_audit_user ON audit_log(user_id);
CREATE INDEX idx_audit_created ON audit_log(created_at);
CREATE INDEX idx_audit_action ON audit_log(action);
```

### API Endpoints(之後 Phase 1 實作)

```
POST   /auth/google/login         # 觸發 OAuth flow
GET    /auth/google/callback      # OAuth callback
POST   /auth/logout               # 登出
GET    /auth/me                   # 看自己狀態

POST   /api/access-requests       # User 申請 access
GET    /api/access-requests/me    # 看自己的申請狀態

GET    /admin/users               # Admin: 看 user 列表
POST   /admin/users/{id}/approve  # Admin: approve
POST   /admin/users/{id}/block    # Admin: block
POST   /admin/users/{id}/unblock  # Admin: unblock
POST   /admin/users/{id}/promote  # Admin: promote to admin

GET    /admin/audit               # Admin: 看 audit log
GET    /admin/audit/export.csv    # Admin: 匯出 CSV
GET    /admin/stats               # Admin: 看流量統計
```