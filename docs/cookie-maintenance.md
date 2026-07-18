# Cookie 維護指南

> StudyArk 要 Google 登入才能完整使用,我們的 server 端需要共用一份有效的 cookies。

---

## ⚠️ 為什麼這份文件重要?

- **cookies 是這個專案的命脈** — 沒 cookies = 整個服務掛掉
- **cookies 千萬不要 commit 到 git**(就算 private repo 也別)
- **cookies 過期就要手動重抓** — Google 登入 cookies 預設有效期約 1-3 個月

---

## 📁 cookies 檔案位置

```
/home/aping/TQark-web/data/cookies/studyark_cookies.json
```

權限:`chmod 600`(只有 owner 可以讀寫)

---

## 🎯 取得 Cookies(一次性 + 過期更新)

### Step 1: 用 Playwright 手動登入

```bash
# SSH 進 server
ssh aping@just4fun.myiphost.com

# 進專案目錄
cd /home/aping/TQark-web

# 啟動 venv
cd backend
source .venv/bin/activate

# 跑 cookie 取得 script(Phase 1 才會寫,這是示意)
python3 -m app.scripts.refresh_cookies
```

`refresh_cookies.py` 會做:
1. 開 Playwright(visible 模式)
2. 開 Google 登入頁
3. **你在視窗裡手動登入**(輸入帳密、2FA 等)
4. 開 StudyArk 確認登入狀態
5. 存 cookies 到 `data/cookies/studyark_cookies.json`
6. 印出「Cookies saved!」

### Step 2: 驗證 cookies 有效

```bash
# 用 FastAPI /health endpoint 驗證
curl -s https://just4fun.myiphost.com:8443/health | python3 -m json.tool
```

看 `cookies_valid` 是否為 `true`、`cookies_expires_at` 還有多久。

---

## 🔄 定期檢查 + 自動 Alert

### /health 自動檢查

FastAPI 啟動時 + 每 10 分鐘跑一次 cookie age check,寫進 `/health` response。

```json
{
  "cookies_valid": true,
  "cookies_expires_at": "2026-09-15T10:00:00",
  "cookies_age_days": 25,
  "cookies_will_expire_soon": false
}
```

### 過期警告(Phase 2 加)

如果 `cookies_expires_at` < 14 天:
- 自動寄信給 admin(用 Google SMTP 或 SendGrid)
- 或寫進 audit log + admin dashboard 顯示 banner

---

## 🚨 Cookies 過期了怎麼辦?

### 症狀
- 搜尋結果突然變空 / 全是登入提示
- `/health` 回 `cookie_status: "expired"` 或 `"missing"`
- User 回報「找不到試卷」

### 解決(15 分鐘 SOP)

1. **SSH 進 server**
2. **跑 refresh script**: `python3 -m app.scripts.refresh_cookies`
3. **手動登入 Google**(在 Playwright 開的視窗)
4. **驗證**:
   ```bash
   curl -s https://just4fun.myiphost.com:8443/health
   ```
5. **測一次搜尋**:
   ```bash
   curl -s "https://just4fun.myiphost.com:8443/api/search?grade=8&year=113&subject=國文" \
     -H "Cookie: session=YOUR_JWT"
   ```
6. **確認 logs 沒異常**

---

## 🔐 安全注意事項

### 絕對不要做
- ❌ 把 cookies commit 到 git(就算 private 也會被吃)
- ❌ 把 cookies 透過 email/Slack 明文傳
- ❌ 把 cookies 上傳到任何第三方服務
- ❌ 把 cookies 路徑 hardcode 在程式碼(用 env var)

### 應該做
- ✅ cookies 檔案權限 `chmod 600`
- ✅ `.gitignore` 已排除(預設有 `data/cookies/` 跟 `*.cookies.json`)
- ✅ 加密備份(用 `age` / `gpg`,密碼存密碼管理器)
- ✅ 定期 rotate(就算沒過期也每 60 天換一次)

### Backup 範例

```bash
# 用 age 加密備份
age -p -o cookies.backup.age /home/aping/TQark-web/data/cookies/studyark_cookies.json
# 會問你密碼,輸入後存到 cookies.backup.age
# 這個檔可以放到外接硬碟 / cloud
```

---

## 📊 監控指標

| 指標 | 健康 | 警告 | 嚴重 |
|------|------|------|------|
| Cookies 存在 | ✅ | - | ❌ 不存在 |
| Google session cookies 數量 | ≥ 3 | 1-2 | 0 |
| 最近 7 天 StudyArk 登入成功 | 100% | 80-99% | < 80% |
| 平均 cookie 年齡 | < 30 天 | 30-60 天 | > 60 天 |

---

## 🔄 之後考慮:OAuth Refresh Token 自動刷新

進階做法(之後評估):
- 用 Google OAuth 流程拿到 refresh token(不用每次登入)
- 用 refresh token 自動生成新的 access token
- 但這需要 StudyArk 沒擋 OAuth flow(之後要測)

短期先用手動刷新 cookies,簡單可靠。