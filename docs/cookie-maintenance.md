# Cookie 維護指南

> StudyArk 要 Google 登入才能完整使用,我們的 server 端需要共用一份有效的 cookies。
> 這份文件說明怎麼取得、儲存、刷新、過期怎麼辦。

---

## ⚠️ 為什麼這份文件重要?

- **cookies 是這個專案的命脈** — 沒 cookies = 整個服務掛掉
- **cookies 千萬不要 commit 到 git**(就算 private repo 也別)
- **cookies 過期就要手動重抓** — Google 登入 cookies 預設有效期約 1-3 個月,看設定

---

## 🎯 取得 Cookies(一次性)

### Step 1: 用 Playwright 手動登入

```bash
# 在要部署的機器上(或本機)
python3 << 'EOF'
from playwright.sync_api import sync_playwright
import json

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)  # 重要:要 visible 才能登入
    context = browser.new_context()
    page = context.new_page()
    
    # 1. 開 Google 登入頁
    page.goto("https://accounts.google.com/")
    
    # 2. 這時你自己手動登入(輸入帳密、2FA 等)
    input("登入完按 Enter...")
    
    # 3. 開 StudyArk 看是不是登入狀態
    page.goto("https://www.studyark.org/")
    if "登入" in page.content():
        print("❌ StudyArk 還沒登入,試試看重新整理")
    else:
        print("✅ StudyArk 登入成功")
    
    # 4. 存 cookies
    cookies = context.cookies()
    with open("/path/to/cookies.json", "w") as f:
        json.dump(cookies, f, indent=2)
    
    print(f"✅ 已存 {len(cookies)} 個 cookies 到 /path/to/cookies.json")
    browser.close()
EOF
```

### Step 2: 放到正確位置

```bash
# 本機開發
cp ~/Downloads/cookies.json backend/data/cookies/studyark_cookies.json

# Docker 部署
cp ~/Downloads/cookies.json ./data/cookies/studyark_cookies.json

# 確認權限
chmod 600 ./data/cookies/studyark_cookies.json
```

---

## 🔄 定期檢查 + 自動 Alert

### 健康檢查 endpoint

```python
# app/services/health.py
import json
from datetime import datetime, timedelta

def check_cookie_age(cookies_path: str) -> dict:
    """回傳 cookies 狀態"""
    try:
        with open(cookies_path) as f:
            cookies = json.load(f)
    except FileNotFoundError:
        return {"status": "missing", "message": "cookies 檔案不存在"}
    except json.JSONDecodeError:
        return {"status": "corrupt", "message": "cookies 檔案格式錯誤"}
    
    # 找 SID/HSID 等 Google session cookies
    google_session_cookies = [c for c in cookies if 'google.com' in c.get('domain', '')]
    
    if not google_session_cookies:
        return {"status": "no_session", "message": "找不到 Google session cookies"}
    
    # 檢查 expires(如果有)
    now = datetime.now().timestamp()
    expiring_soon = []
    for c in google_session_cookies:
        expires = c.get('expires', -1)
        if expires > 0:
            days_left = (expires - now) / 86400
            if days_left < 14:
                expiring_soon.append({
                    "name": c['name'],
                    "days_left": round(days_left, 1)
                })
    
    return {
        "status": "ok" if not expiring_soon else "expiring_soon",
        "total_cookies": len(cookies),
        "google_session_cookies": len(google_session_cookies),
        "expiring_soon": expiring_soon,
    }
```

### 接 cron + alert(之後實作)

```bash
# 每天早上 9 點跑一次健康檢查
0 9 * * * cd /path/to/TQark-web && \
  python3 backend/scripts/check_health.py || \
  echo "StudyArk cookies 異常,請手動檢查" | mail -s "⚠️ StudyArk Alert" william@example.com
```

---

## 🚨 Cookies 過期了怎麼辦?

### 症狀
- 搜尋結果突然變空 / 全是登入提示
- `/health` endpoint 回 `cookie_status: "expired"` 或 `"missing"`
- 使用者回報「找不到試卷」

### 解決(15 分鐘 SOP)

1. **SSH 進部署的機器**
2. **跑 refresh script**(之後會寫,先用上面 Step 1 的 Playwright 流程)
3. **驗證**:`curl http://localhost:8000/api/search?grade=8&year=113&semester=2&subject=國文`
4. **確認 logs 沒異常**

---

## 🔐 安全注意事項

### 絕對不要做
- ❌ 把 cookies commit 到 git(就算 public 也會被吃)
- ❌ 把 cookies 透過 email/Slack 明文傳
- ❌ 把 cookies 上傳到任何第三方服務(除了部署機器本身)

### 應該做
- ✅ cookies 檔案權限 `chmod 600`
- ✅ `.gitignore` 已排除(`*.cookies.json` 跟 `data/cookies/`)
- ✅ 加密備份(用 age / gpg 之類,密碼存密碼管理器)
- ✅ 定期 rotate(就算沒過期也每 60 天換一次)

---

## 📊 監控指標(理想狀態)

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