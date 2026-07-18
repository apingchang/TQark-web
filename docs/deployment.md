# 部屬指南

> 完整 step-by-step,涵蓋家裡主機 + DDNS + Caddy + FastAPI + systemd。

---

## 🧱 環境需求

| 工具 | 版本 | 用途 |
|------|------|------|
| Python | 3.12+ | Backend |
| Caddy | 2.7+ | Reverse proxy + 自動 HTTPS |
| systemd | 內建 | Process manager |
| git | 2.40+ | 版本控制 |
| Playwright Chromium | (自動裝) | 抓 StudyArk |

**為什麼選 Caddy 不選 Nginx**:
- ✅ 自動 HTTPS(Let's Encrypt cert 自動 renew,不用 cron)
- ✅ 設定檔 3 行搞定(Nginx 要 20 行)
- ✅ 預設安全 headers
- ✅ 對 DDNS 友善

**為什麼不選 Docker**:
- 直接 Python + systemd 比較簡單
- 跟 OpenClaw 部署方式一致
- 少一層抽象

---

## 🌐 1. DDNS 確認

確認 DDNS 已經設定好,並指向家裡的對外 IP:

```bash
# 檢查 DDNS 解析
nslookup just4fun.myiphost.com
# 期望:1.162.10.217(或你家對外 IP)

# 確認對外 IP
curl -s https://api.ipify.org
# 期望跟上面一樣
```

---

## 🔌 2. Router Port Forwarding 設定

**進你家 router admin UI**(通常是 `http://192.168.1.1` 或 `http://192.168.50.1`)

找到 **Port Forwarding / 虛擬伺服器 / NAT** 設定,加一條:

| 欄位 | 值 |
|------|-----|
| Service name | TQark-web |
| External port | **8443** |
| Internal port | **8443** |
| Internal IP | **192.168.50.31**(Linux 主機) |
| Protocol | TCP |

**儲存 + 套用**(有些 router 要重啟才生效)。

驗證:

```bash
# 從機器本地測
nc -z -w 3 192.168.50.31 8443 && echo "OK" || echo "FAIL"

# 從外部測(等幾秒讓 router 套用)
nc -z -w 5 just4fun.myiphost.com 8443 && echo "✅ 8443 通" || echo "❌ 還是不通"
```

如果**還是不通**:
- 確認 Linux 上 firewall 沒擋(`sudo ufw status`、`sudo iptables -L -n`)
- 確認 router 設的 internal IP 是這台 Linux 的 IP(用 `hostname -I` 看)
- 重啟 router 再試

---

## 📦 3. Clone + 設定專案

```bash
# Clone
cd /home/aping
git clone git@github.com:apingchang/TQark-web.git
cd TQark-web

# 建資料目錄
mkdir -p data/cookies data/pdfs data/db data/uploads
chmod 700 data/cookies

# Python venv(之後會用,但 Phase 1 還沒 code,先做架構)
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 裝 Playwright browser
playwright install chromium
playwright install-deps chromium
```

---

## 🔐 4. Google OAuth 設定

**完整 step-by-step 在 [`google-oauth-setup.md`](google-oauth-setup.md),這邊只列重點:**

1. 去 https://console.cloud.google.com/
2. 建(或選)專案
3. **APIs & Services → Credentials → Create Credentials → OAuth client ID**
4. **Application type: Web application**
5. **Authorized JavaScript origins**: `https://just4fun.myiphost.com:8443`
6. **Authorized redirect URIs**: `https://just4fun.myiphost.com:8443/auth/google/callback`
7. 拿到 **Client ID** + **Client Secret**

存到 `/home/aping/MyProjects/TQark-web/backend/.env`:

```bash
GOOGLE_CLIENT_ID=xxx.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-xxx
ADMIN_EMAILS=your.email@gmail.com  # 第一個 admin
JWT_SECRET=$(openssl rand -hex 32)   # 隨機 JWT 簽章密鑰
COOKIES_PATH=/home/aping/MyProjects/TQark-web/data/cookies/studyark_cookies.json
DB_PATH=/home/aping/MyProjects/TQark-web/data/db/app.db
PDF_CACHE_DIR=/home/aping/MyProjects/TQark-web/data/pdfs
LOG_DIR=/home/aping/MyProjects/TQark-web/data/logs
PORT=8000
ENV=production
```

```bash
chmod 600 /home/aping/MyProjects/TQark-web/backend/.env
```

---

## 🍪 5. StudyArk Cookies 取得

**完整 step-by-step 在 [`cookie-maintenance.md`](cookie-maintenance.md)**(之後會更新指向真實 cookies 流程)。

簡述:
1. 跑 Playwright 開瀏覽器
2. 手動登入 Google + StudyArk
3. 存 cookies 到 `/home/aping/MyProjects/TQark-web/data/cookies/studyark_cookies.json`

---

## 🚀 6. systemd Service 設定

建立 `/etc/systemd/system/tqark-web.service`:

```ini
[Unit]
Description=TQark Web - StudyArk archive downloader
After=network.target

[Service]
Type=simple
User=aping
Group=aping
WorkingDirectory=/home/aping/MyProjects/TQark-web/backend
Environment="PATH=/home/aping/MyProjects/TQark-web/backend/.venv/bin"
EnvironmentFile=/home/aping/MyProjects/TQark-web/backend/.env
ExecStart=/home/aping/MyProjects/TQark-web/backend/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 2
Restart=always
RestartSec=5
StandardOutput=append:/home/aping/MyProjects/TQark-web/data/logs/backend.log
StandardError=append:/home/aping/MyProjects/TQark-web/data/logs/backend.log

# 資源限制(保護 server)
LimitNOFILE=65536
MemoryMax=2G

[Install]
WantedBy=multi-user.target
```

啟用:

```bash
sudo systemctl daemon-reload
sudo systemctl enable tqark-web
sudo systemctl start tqark-web
sudo systemctl status tqark-web
```

---

## 🌐 7. Caddy 安裝 + 設定

### 安裝 Caddy

```bash
# Debian/Ubuntu 一鍵安裝
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/deb/debian.any-version.any-arch.pkgs.sudo apt install caddy' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update
sudo apt install caddy
```

### 設定 Caddyfile

建立 `/etc/caddy/Caddyfile`:

```caddyfile
# TQark Web - 8443 HTTPS
just4fun.myiphost.com:8443 {
    # 自動 HTTPS(Let's Encrypt 透過 DNS-01 或 HTTP-01)
    # Caddy 會自動選最佳 challenge
    
    # Reverse proxy 到 FastAPI
    reverse_proxy 127.0.0.1:8000 {
        header_up Host {host}
        header_up X-Real-IP {remote}
        header_up X-Forwarded-For {remote}
        header_up X-Forwarded-Proto {scheme}
    }
    
    # 安全 headers
    header {
        # HSTS(強制 HTTPS)
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        # 防止 clickjacking
        X-Frame-Options "SAMEORIGIN"
        # 防 XSS
        X-Content-Type-Options "nosniff"
        # CSP
        Content-Security-Policy "default-src 'self'; img-src 'self' data: https:; script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com; style-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com; connect-src 'self' https://www.googleapis.com; frame-ancestors 'none'"
        # Referrer
        Referrer-Policy "strict-origin-when-cross-origin"
        # Permissions
        Permissions-Policy "geolocation=(), microphone=(), camera=()"
    }
    
    # 隱藏 Caddy 資訊
    # (預設就有,這邊只是註解提醒)
    
    # Access log
    log {
        output file /var/log/caddy/tqark-access.log {
            roll_size 100mb
            roll_keep 5
        }
    }
}

# 可選:把 8443 也提供 health check endpoint
# http://just4fun.myiphost.com:8443/health -> backend /health
```

### Caddy 需要 sudo port(80 or 8443)

Caddy 預設需要 bind 到 privileged port(< 1024)時要 root。
但 8443 是 unprivileged,所以可以直接用 `aping` user 跑。

如果用 systemd:

```bash
# Caddy 已經有預設 systemd unit
sudo systemctl enable caddy
sudo systemctl start caddy
sudo systemctl status caddy
```

### Caddy log 目錄

```bash
sudo mkdir -p /var/log/caddy
sudo chown caddy:caddy /var/log/caddy
```

### 驗證 HTTPS cert 自動申請

```bash
sudo journalctl -u caddy -f
# 應該看到:"obtaining certificate", "certificate obtained successfully"
```

如果 cert 申請失敗(常見原因):
- DDNS 還沒生效(等 5-10 分鐘)
- Port 80 被擋 → 確認 router 上 80 也開放(Let's Encrypt HTTP-01 需要)
  - **或**:改用 DNS-01 challenge(需要 DDNS 提供 API)

**我們目前 80 blocked,所以要確認 Caddy 用 DNS-01**:
- Caddy 預設嘗試 HTTP-01 → 失敗 → 自動 fallback 到其他方法
- 如果 DDNS 提供 API,可以設定 `dns` directive
- **目前 myiphost.com 不知道有沒有 API,可能要 fallback 到其他解法**

詳見 [`google-oauth-setup.md`](google-oauth-setup.md) 跟 [`security-model.md`](security-model.md)。

---

## 🧪 8. 整體驗證

```bash
# 1. FastAPI 跑起來沒?
curl -s http://127.0.0.1:8000/health
# 期望:{"status":"ok"}

# 2. Caddy proxy 通嗎?
curl -sI https://just4fun.myiphost.com:8443/health
# 期望:HTTP/2 200

# 3. 從外部瀏覽器測
# 打開 https://just4fun.myiphost.com:8443/
# 看到 landing page + Sign in with Google
```

---

## 🔄 9. 更新流程

之後改 code:

```bash
cd /home/aping/MyProjects/TQark-web
git pull

# 如果 requirements.txt 變了
cd backend
source .venv/bin/activate
pip install -r requirements.txt

# 重啟 service
sudo systemctl restart tqark-web
sudo systemctl status tqark-web

# 看 logs
tail -f /home/aping/MyProjects/TQark-web/data/logs/backend.log
```

---

## 🔍 10. 故障排除

### 8443 連不到?
1. `nc -z just4fun.myiphost.com 8443` 測試
2. 確認 router port forwarding 設好
3. 確認 Caddy 跑起來(`systemctl status caddy`)
4. 確認 FastAPI 跑起來(`systemctl status tqark-web`)
5. 看 Caddy log:`sudo journalctl -u caddy -n 50`

### Google OAuth 失敗?
1. 看 logs: `tail -f /home/aping/MyProjects/TQark-web/data/logs/backend.log`
2. 確認 `.env` 有 `GOOGLE_CLIENT_ID` + `GOOGLE_CLIENT_SECRET`
3. 確認 Google Cloud Console redirect URI 對:
   `https://just4fun.myiphost.com:8443/auth/google/callback`

### Search 回空?
1. 確認 StudyArk cookies 沒過期(詳見 `cookie-maintenance.md`)
2. 確認 Playwright 跑得起來(可以 SSH 進去看)

### 504 Gateway Timeout?
1. FastAPI 沒回應
2. `systemctl restart tqark-web`
3. 看 backend.log

---

## 📋 上線前 Checklist

- [ ] Router port forwarding 設好(8443 → 192.168.50.31:8443)
- [ ] `nc -z just4fun.myiphost.com 8443` 通
- [ ] Caddy 跑起來 + cert 拿到
- [ ] FastAPI 跑起來 + `/health` 回 200
- [ ] Google OAuth 設定完成 + Client ID/Secret 存到 .env
- [ ] StudyArk cookies 取得 + 存到 `data/cookies/`
- [ ] 用你的 Google 帳號登入 → 看到 admin dashboard
- [ ] 測一次「申請 access → admin approve → 登入 → 搜尋 → 下載」完整流程
- [ ] log rotation 設好(不要讓 log 把磁碟塞爆)
- [ ] backup cron 設好(DB + cookies 定期備份)