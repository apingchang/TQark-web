# Google OAuth 設定指南

> Step-by-step 帶你建立 Google OAuth Client,讓 TQark-web 用 Google 登入。

---

## 🎯 為什麼用 Google OAuth?

| 好處 | 說明 |
|------|------|
| ✅ **不用自己管密碼** | Google 幫你驗證身份,我們只看 email |
| ✅ **減少資安風險** | 我們不存密碼,只存 Google 給的 user info |
| ✅ **使用者體驗好** | 一鍵登入,不用記密碼 |
| ✅ **免費** | Google OAuth 完全免費 |

---

## ⏱ 預估時間:**15 分鐘**

---

## Step 1: 開 Google Cloud Console

1. 打開瀏覽器到: **https://console.cloud.google.com/**
2. 用你的 Google 帳號登入(就是你要當 admin 的那個)
3. 如果是第一次用,會要你同意服務條款

---

## Step 2: 建(或選)一個專案

1. 點左上角專案下拉(可能寫 "My First Project" 之類)
2. 點 **「New Project」**(新建專案)
3. 填寫:
   - **Project name**: `TQark-web`(或你喜歡的名字)
   - **Location**: 留預設(「No organization」)
4. 點 **「Create」**(建立)
5. 等幾秒,左上角下拉切換到剛建的 `TQark-web` 專案

---

## Step 3: 設定 OAuth Consent Screen

OAuth Consent Screen 是第一次登入時,Google 會給使用者看的同意畫面。

1. 左邊選單 → **APIs & Services** → **OAuth consent screen**
2. 選 User type:
   - **External**(給你的同事用,他們也有 Google 帳號)
   - 不要選 Internal(那是 G Suite 企業限定)
3. 點 **「Create」**
4. 填寫 App information:
   - **App name**: `TQark-web`(會顯示在 Google 同意畫面)
   - **User support email**: 你的 email(給 user 聯絡用)
   - **App logo**: 可選,跳過
5. 填寫 App domain:
   - **Application home page**: `https://just4fun.myiphost.com:8443`
   - **Application privacy policy**: 留空(私人專案)
   - **Application terms of service**: 留空
6. 填寫 Developer contact:
   - **Email addresses**: 你的 email
7. 點 **「Save and Continue」**
8. **Scopes**: 不用動,跳過 → 點 **「Save and Continue」**
9. **Test users**: 加上你同事的 email(之後可以加更多)
   - 點 **「Add Users」** → 輸入 email
10. 點 **「Save and Continue」**
11. 回到主畫面,看到 **「Publishing status」** 顯示 **「Testing」**
    - 沒關係,Testing 模式可以加 100 個 test user
    - 之後 user 數量穩定了可以 publish

---

## Step 4: 建 OAuth Client ID

1. 左邊選單 → **APIs & Services** → **Credentials**
2. 點上方 **「+ Create Credentials」** → **「OAuth client ID」**
3. 填寫:
   - **Application type**: **Web application**
   - **Name**: `TQark-web` 或 `TQark-web (Linux server)`
4. **Authorized JavaScript origins**:
   - 點 **「+ Add URI」**
   - 填: `https://just4fun.myiphost.com:8443`
5. **Authorized redirect URIs**:
   - 點 **「+ Add URI」**
   - 填: `https://just4fun.myiphost.com:8443/auth/google/callback`
6. 點 **「Create」**
7. 🎉 **彈窗顯示 Client ID + Client Secret**
   - **Client ID**: `xxx.apps.googleusercontent.com`(是公開的)
   - **Client Secret**: `GOCSPX-xxx`(**是機密,不能給任何人**)

---

## Step 5: 儲存到 .env

```bash
# 進到 TQark-web 專案
cd /home/aping/TQark-web/backend

# 編輯 .env(如果還沒建,先從 .env.example 複製)
cp .env.example .env
nano .env  # 或 vim / VSCode
```

填入:

```bash
# === Google OAuth ===
GOOGLE_CLIENT_ID=xxx.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-xxxxxxxxxxxxxxxxx

# === Admin 設定 ===
# 第一個 admin 的 email(用逗號分隔可加多個)
ADMIN_EMAILS=your.email@gmail.com

# === JWT 簽章密鑰 ===
# 用這個指令產生隨機密鑰:
JWT_SECRET=PASTE_GENERATED_SECRET_HERE

# === 其他設定 ===
COOKIES_PATH=/home/aping/TQark-web/data/cookies/studyark_cookies.json
DB_PATH=/home/aping/TQark-web/data/db/app.db
PDF_CACHE_DIR=/home/aping/TQark-web/data/pdfs
LOG_DIR=/home/aping/TQark-web/data/logs
PORT=8000
ENV=production
```

產生 JWT secret:

```bash
openssl rand -hex 32
# 複製輸出,貼到 .env 的 JWT_SECRET
```

**保護 .env**:

```bash
chmod 600 /home/aping/TQark-web/backend/.env
# 確認 .gitignore 有排除 .env(預設已有)
grep "^\.env$" /home/aping/TQark-web/.gitignore && echo "✅ 已排除"
```

---

## Step 6: 驗證 OAuth 設定

**Phase 1 還沒寫 code,所以這步等 Phase 1 完成才能測。**

到時候的測試流程:

1. 打開瀏覽器到 `https://just4fun.myiphost.com:8443/`
2. 看到 landing page + 「Sign in with Google」按鈕
3. 點按鈕 → 跳到 Google → 選帳號 → 同意授權
4. 回到我們網站 → 應該看到「歡迎,你的名字」

如果失敗:

| 錯誤訊息 | 原因 |
|---------|------|
| `redirect_uri_mismatch` | Google Cloud Console 設的 redirect URI 跟實際不符,檢查大小寫跟 port |
| `access_denied` | 使用者按取消,或 App 還在 Testing mode 但 email 不在 test user 列表 |
| `invalid_client` | Client ID 或 Secret 設錯,檢查 .env |

---

## 🔒 安全注意事項

### Client Secret 是機密!
- ❌ **絕對不要** commit 到 git
- ❌ **絕對不要** 貼到 Slack/Email/公開 forum
- ❌ **絕對不要** 寫在程式碼裡(hardcode)
- ✅ 只放在 `.env` 檔,權限 `chmod 600`
- ✅ 如果不小心洩漏,到 Google Cloud Console **重設**

### 定期 rotate
- 每 6 個月換一次 Client Secret
- 流程:Google Cloud Console → Credentials → 你的 OAuth client → Reset secret → 更新 .env → 重啟服務

### 如果 DDNS domain 換了
- 要去 Google Cloud Console 改 Authorized URIs
- 不改會壞

---

## 📋 Checklist

- [ ] Google Cloud Console 建好 TQark-web 專案
- [ ] OAuth Consent Screen 設好(External + Testing)
- [ ] OAuth Client ID 建好
- [ ] Authorized JavaScript origins 填對(8443)
- [ ] Authorized redirect URIs 填對(含 `/auth/google/callback`)
- [ ] Client ID + Secret 存到 `.env`
- [ ] `chmod 600 .env` 設好
- [ ] JWT secret 用 `openssl rand -hex 32` 產生
- [ ] ADMIN_EMAILS 設好你的 email
- [ ] `.gitignore` 排除 `.env`

---

## 🆘 故障排除

### 「This app isn't verified」
- 因為 App 在 Testing mode,Google 會警告「未驗證的 app」
- 第一次會看到警告頁,點「Advanced」→「Go to TQark-web (unsafe)」即可
- 之後正式上線可以送 Google 審核變 verified(但個人用途不需要)

### 「Access blocked: This app's request is invalid」
- 通常是 OAuth Consent Screen 設定缺東西
- 回到 Step 3 重檢查

### 「redirect_uri_mismatch」
- Authorized redirect URIs 沒設到完整的 callback URL
- 要包含 port + path,例:`https://just4fun.myiphost.com:8443/auth/google/callback`

### 「Access denied - TQark-web has not completed the Google verification process」
- 你是用 non-test-user 的 Google 帳號登入
- 解法:把他加進 OAuth Consent Screen 的 Test users

---

## 📚 參考

- Google 官方文件:https://developers.google.com/identity/protocols/oauth2/web-server
- OAuth 2.0 概念:https://oauth.net/2/