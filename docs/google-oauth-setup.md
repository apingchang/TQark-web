# Google OAuth 設定指南(中文介面版)

> Step-by-step 帶你建立 Google OAuth Client,讓 TQark-web 用 Google 登入。
> **2026 版 Google Auth Platform 介面**(2024 後改版,跟舊的 `APIs & Services → OAuth consent screen` 不一樣)

---

## 📋 中英對照表(必看!)

Google Cloud Console 2024 改版後,介面跟舊文件差很多。對照表讓你對應:

| 英文(我舊版的寫法) | 中文(現在介面) |
|-------------------|----------------|
| OAuth consent screen | **品牌** ← 在左邊選單 |
| Branding | **品牌** |
| Audience | **目標對象** |
| Clients | **用戶端** ← 在這裡建 OAuth Client |
| Data Access | **資料存取權** |
| Verification Center | **驗證中心** |
| Application name | **應用程式名稱** |
| User support email | **使用者支援電子郵件** |
| App logo | **應用程式標誌** |
| App domain | **應用程式網域** |
| Application home page | **應用程式首頁** |
| Application privacy policy link | **應用程式隱私權政策連結** |
| Application terms of service link | **應用程式服務條款連結** |
| Developer contact | **開發人員聯絡資訊** |
| Authorized JavaScript origins | **已授權的 JavaScript 來源** |
| Authorized redirect URIs | **已授權的重新導向 URI** |
| Web application | **網頁應用程式** |
| Application type | **應用程式類型** |
| Client ID | **用戶端 ID** |
| Client secret | **用戶端密鑰** |
| Create credentials | **建立憑證** |
| OAuth client ID | **OAuth 用戶端 ID** |
| External | **外部** |
| Internal | **內部**(只 G Suite 企業能用) |
| Test users | **測試使用者** |
| Scopes | **範圍** |
| Save and Continue | **儲存並繼續** |

---

## ⏱ 預估時間:**15 分鐘**

---

## Step 1: 開 Google Cloud Console

1. 瀏覽器到 **https://console.cloud.google.com/**
2. 用你要當 admin 的 Google 帳號登入
3. 切換到 **TQark-web** 專案(你已經建了 ✅)

---

## Step 2: 進到 Google Auth Platform

從左上角漢堡選單 ☰ 點開,選 **「API 和服務」**(APIs & Services)→ 然後找 **「OAuth 同意畫面」** 或直接從搜尋框輸入 **「Google Auth Platform」**。

> 2024 之後的新介面,進到後左邊選單會看到:**總覽 / 品牌 / 目標對象 / 用戶端 / 資料存取權 / 驗證中心 / 設定**

---

## Step 3: 設定「品牌」(Branding)

**你在這個畫面** ✅(從截圖確認)

填寫:

| 欄位 | 填什麼 | 說明 |
|------|--------|------|
| **應用程式名稱** | `TQark-web` | 會顯示在 Google 登入同意畫面 |
| **使用者支援電子郵件** | 你的 email(從截圖看是 `apingchang@gmail.com`) | User 有問題可以聯絡 |
| **應用程式標誌** | **跳過** | 私人專案不用 |
| **應用程式網域** | | |
| → 應用程式首頁 | `https://just4fun.myiphost.com:8443` | 你的 DDNS + port |
| → 應用程式隱私權政策連結 | **留空** | 私人專案不需 |
| → 應用程式服務條款連結 | **留空** | 私人專案不需 |
| **開發人員聯絡資訊**(捲到下面) | 你的 email | Google 審核用 |

**完成後**:
- 點頁面最下面(或右上)的 **「儲存並繼續」**(Save and Continue)按鈕
- 會跳到下一個分頁:**目標對象**

> ⚠️ 你截圖右下角說「OAuth 設定建立完成!」,這個是說「OAuth 設定」這個模組已啟用,**還沒完成所有設定**,要繼續走完下面步驟。

---

## Step 4: 設定「目標對象」(Audience)

這一頁問你「這 App 給誰用」。

### 4.1 選使用者類型
- **外部 (External)** ← 選這個
- 不要選內部(那是 Google Workspace 企業限定,要付費 G Suite)

### 4.2 測試使用者(Test users)

在 Testing 模式下,**只有你列在這裡的 email 才能登入**。

**加上你 + 你同事的 email**(之後還可以再加):
1. 點 **「新增使用者」**(Add users)按鈕
2. 輸入 email,Enter
3. 重複加多個
4. 至少加你自己的 admin email

### 4.3 儲存
- 點 **「儲存並繼續」**

---

## Step 5: 設定「資料存取權」(Data Access)

這頁是 Scopes(你的 App 要存取 user 的什麼資料)。

### 5.1 非機密範圍(Your non-sensitive scopes)

**加上這三個**:
- `openid` - 連結你的 Google 身份
- `https://www.googleapis.com/auth/userinfo.email` - 拿 email
- `https://www.googleapis.com/auth/userinfo.profile` - 拿名字、頭像

**怎麼加**:
1. 點「新增或移除範圍」按鈕
2. 在「手動新增範圍」輸入上述三個 URI,一個一個加
3. 或從清單裡手動勾「openid」、「userinfo.email」、「userinfo.profile」
4. 點「更新」/「儲存」

### 5.2 機密範圍(Your sensitive scopes)

**不要加任何東西**,留空。

「機密」指的是存取 Google Drive、Gmail、Calendar 等個資的範圍,例如 `drive.readonly`、`gmail.readonly`。
TQark-web 完全不需要這些資料。

### 5.3 受限制範圍(Your restricted scopes)

**不要加任何東西**,留空。

「受限制」是更敏感的範圍,需要 Google 安全審核。
TQark-web 完全不需要。

### 5.4 為什麼不設機密/受限制?

| 理由 | 說明 |
|------|------|
| **不需要** | TQark-web 只要 email + name + 頭像就能運作 |
| **最小權限原則** | Scope 越少,User 看到 Google 同意畫面時越安心 |
| **免審核** | 機密/受限制 scope 要送 Google 安全審核,曠日廢時 |

### 5.5 儲存

點左下藍色 **「Save」(儲存)** 按鈕 → 進到「用戶端」分頁

---

## Step 6: 建立「用戶端」(OAuth Client ID)

**這是最關鍵的步驟** ⚠️

### 6.1 點進「用戶端」分頁(左邊選單)

### 6.2 點「+ 建立用戶端」(Create client)按鈕

### 6.3 填寫

| 欄位 | 填什麼 |
|------|--------|
| **應用程式類型** | **網頁應用程式**(Web application) |
| **名稱** | `TQark-web` 或 `TQark-web (Linux server)` |

### 6.4 「已授權的 JavaScript 來源」(Authorized JavaScript origins)

點「+ 新增 URI」,填:

```
https://just4fun.myiphost.com:8443
```

### 6.5 「已授權的重新導向 URI」(Authorized redirect URIs)

點「+ 新增 URI」,填:

```
https://just4fun.myiphost.com:8443/auth/google/callback
```

### 6.6 點「建立」(Create)

### 6.7 🎉 拿到 Client ID + Client Secret

彈窗會顯示:

| 欄位 | 範例 | 能不能公開? |
|------|------|------------|
| **用戶端 ID** | `xxx.apps.googleusercontent.com` | ✅ 可以公開 |
| **用戶端密鑰** | `GOCSPX-xxx` | ❌ **絕對機密** |

**複製兩個值**,等等要存到 `.env`。

---

## Step 7: 儲存到 .env

```bash
# 進到 TQark-web 專案(等 Phase 1 coding 才會有,先記住位置)
cd /home/aping/MyProjects/TQark-web/backend

# 編輯 .env
cp .env.example .env
nano .env  # 或 vim / VSCode
```

填入:

```bash
# === Google OAuth ===
GOOGLE_CLIENT_ID=xxx.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-xxx

# === Admin 設定 ===
ADMIN_EMAILS=apingchang@gmail.com

# === JWT 簽章密鑰 ===
JWT_SECRET=__TODO_GENERATE__

# === 其他 ===
COOKIES_PATH=/home/aping/MyProjects/TQark-web/data/cookies/studyark_cookies.json
DB_PATH=/home/aping/MyProjects/TQark-web/data/db/app.db
PDF_CACHE_DIR=/home/aping/MyProjects/TQark-web/data/pdfs
LOG_DIR=/home/aping/MyProjects/TQark-web/data/logs
PORT=8000
ENV=production
```

產生 JWT secret:

```bash
openssl rand -hex 32
# 複製輸出貼到 JWT_SECRET=
```

保護 .env:

```bash
chmod 600 /home/aping/MyProjects/TQark-web/backend/.env
```

---

## 🆘 你現在卡在哪裡?

按你的進度,可能停在這幾個地方:

### A. 卡在「品牌」畫面(像你的截圖)
**解法**:
1. 把「應用程式網域」那區的「應用程式首頁」填上 `https://just4fun.myiphost.com:8443`
2. 捲到最下面填「開發人員聯絡資訊」(你 email)
3. 點「**儲存並繼續**」
4. 會跳到「目標對象」

### B. 「儲存並繼續」按鈕在哪?
- 通常在頁面**最下面**,捲到底找
- 或頁面**右上角**
- 有時候是藍色按鈕,有時候是灰色

### C. 在「目標對象」卡住
**解法**:
- 選「外部」
- 加你自己 email 為測試使用者
- 「儲存並繼續」

### D. 已經走完 Step 3-5,但不會建 Client
**這就是 Step 6,最關鍵**:
- 從左邊選單點「**用戶端**」
- 點「**+ 建立用戶端**」
- 選「**網頁應用程式**」
- 填 redirect URI(上面 Step 6.5)
- 建立 → 拿到 Client ID + Secret

### E. 拿到 Client ID/Secret 但不知道存哪
- 先複製到一個**臨時文字檔**(例如 `/tmp/google_creds.txt`)
- 等 Phase 1 coding 完,我會建 `.env` 檔案給你

---

## 🔒 安全注意事項

### Client Secret 是機密!
- ❌ **絕對不要** commit 到 git
- ❌ **絕對不要** 貼到 Slack/Email/公開 forum
- ❌ **絕對不要** 寫在程式碼裡(hardcode)
- ✅ 只放在 `.env` 檔,權限 `chmod 600`
- ✅ 如果不小心洩漏,到 Google Cloud Console **重設**(Credentials → 你的 OAuth client → RESET SECRET)

### ⚠️ 不要點「下載 JSON」按鈕!

Google Cloud Console 在「用戶端」分頁的右邊會有一個「**下載 JSON**」按鈕。**這個 JSON 包含你的 Client Secret**,下載後**絕對不要 commit 到 git**。

**如果不小心下載了**:
- ✅ 把內容讀出來(那個 JSON 內含 client_id + client_secret)
- ✅ 把值貼到 `.env` 對應欄位
- ❌ **不要把 .json 檔放到任何會被 git 追蹤的資料夾**
- ❌ 更不要 `git add` 或 commit 它
- ✅ **直接刪掉 .json 檔**(用完就刪)

**為什麼 GitHub 會擋下 push**:
GitHub 2024 之後啟用 push protection,任何 commit 含 Google/AWS/etc. 的 secret 都會被擋下。**別繞過**(「Allow secret」按鈕),重設 secret 比較安全。

### 「這個 App 未經驗證」警告
- 因為你的 App 在 Testing mode,Google 會警告
- User 第一次登入會看到警告頁,點「進階」→「前往 TQark-web(不安全)」即可
- 之後正式上線可以送 Google 審核變 verified(但個人用途不需要)

---

## ✅ 完成的檢查清單

- [ ] 品牌設定完成(應用程式名稱 + email + 首頁)
- [ ] 目標對象 = 外部
- [ ] 測試使用者加了你的 email
- [ ] 用戶端(Clients)分頁建好 OAuth Client ID
- [ ] 「已授權的重新導向 URI」有 `https://just4fun.myiphost.com:8443/auth/google/callback`
- [ ] 拿到 **用戶端 ID** + **用戶端密鑰**(複製存到 `/tmp/` 或貼在備忘)
- [ ] **不要貼在這個對話裡**(Secret 是機密!)

---

## 📸 預期畫面對照

| 你在這裡 | 應該看到 |
|---------|---------|
| **左邊選單** | 看到「總覽 / 品牌 / 目標對象 / 用戶端 / 資料存取權 / 驗證中心 / 設定」 |
| **品牌分頁** | 應用程式名稱已填 TQark-web,Email 是你的 |
| **用戶端分頁** | 看到你剛建的 Client(名稱可能是 TQark-web),右邊有 Client ID |

---

**需要我陪你一步步走嗎?** 如果你卡在某個步驟,把截圖丟給我,我直接告訴你該按什麼。

或者你已經走完 Step 6 拿到 Client ID + Secret,跟我說一聲,我可以幫你:
- 驗證設定對不對(Client ID 格式、redirect URI 對應)
- 等 Phase 1 coding 開始時幫你建 `.env`

**不要把 Client Secret 直接貼給我** ❌(機密!),只貼 Client ID(可以公開)讓我驗證格式就好。