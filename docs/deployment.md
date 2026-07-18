# 部屬指南

> 詳細的部署步驟,涵蓋本機開發、Docker、自架 VPS、Cloudflare Tunnel。

---

## 🧱 環境需求

| 工具 | 版本 | 用途 |
|------|------|------|
| Python | 3.12+ | Backend |
| Node.js | 20+ | Playwright 內建(其實不需要,但方便) |
| Docker | 24+ | 容器化(推薦) |
| Docker Compose | 2.x | 一鍵起服務 |
| Git | 2.40+ | 版本控制 |

---

## 💻 選項 A:本機開發(純 Python,不用 Docker)

```bash
# 1. clone repo
git clone https://github.com/apingchang/TQark-web.git
cd TQark-web

# 2. 建立 Python venv
cd backend
python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# 3. 裝依賴
pip install -r requirements.txt
playwright install chromium

# 4. 設定環境變數
cp .env.example .env
# 編輯 .env,填上 StudyArk cookies 路徑(參考 cookie-maintenance.md)

# 5. 起 FastAPI(開發模式,有 auto-reload)
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# 6. 開另一個 terminal 起前端靜態檔案
cd ../frontend
python3 -m http.server 5500

# 7. 開瀏覽器
open http://localhost:5500
```

---

## 🐳 選項 B:Docker Compose(推薦)

```bash
# 1. clone repo
git clone https://github.com/apingchang/TQark-web.git
cd TQark-web

# 2. 準備資料持久化目錄
mkdir -p data/cookies data/pdfs data/db

# 3. 把 StudyArk cookies 放到 ./data/cookies/studyark_cookies.json
#    參考 cookie-maintenance.md

# 4. 起服務
docker compose -f deploy/docker-compose.yml up -d

# 5. 看 logs
docker compose -f deploy/docker-compose.yml logs -f

# 6. 驗證
curl http://localhost:8000/health
open http://localhost:8000
```

### docker-compose.yml 預定內容(待寫)

```yaml
version: '3.8'
services:
  backend:
    build: ./backend
    ports:
      - "8000:8000"
    volumes:
      - ./data/cookies:/app/data/cookies:ro
      - ./data/pdfs:/app/data/pdfs
      - ./data/db:/app/data/db
    environment:
      - COOKIES_PATH=/app/data/cookies/studyark_cookies.json
      - DB_PATH=/app/data/db/metadata.db
      - PDF_CACHE_DIR=/app/data/pdfs
      - RATE_LIMIT_PER_MIN=10
    restart: unless-stopped

  nginx:
    image: nginx:alpine
    ports:
      - "443:443"
      - "80:80"
    volumes:
      - ./frontend:/usr/share/nginx/html:ro
      - ./deploy/nginx/nginx.conf:/etc/nginx/nginx.conf:ro
      - ./deploy/nginx/conf.d:/etc/nginx/conf.d:ro
    depends_on:
      - backend
    restart: unless-stopped
```

---

## ☁️ 選項 C:Cloudflare Tunnel(免費對外)

這是給家用主機對外的推薦方式。

### Step 1: 安裝 cloudflared

```bash
# 在要對外的主機上(Ubuntu/Debian)
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
  | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared focal main' \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt update
sudo apt install cloudflared
```

### Step 2: 登入 Cloudflare

```bash
cloudflared tunnel login
# 會跳瀏覽器,選你的 domain
```

### Step 3: 建 tunnel

```bash
cloudflared tunnel create TQark-web
# 會給你一個 tunnel UUID + credentials-file 路徑
```

### Step 4: 設定 config.yml

```yaml
# ~/.cloudflared/config.yml
tunnel: <TUNNEL_UUID>
credentials-file: /home/aping/.cloudflared/<TUNNEL_UUID>.json

ingress:
  - hostname: studyark.example.com   # 換成你的 domain
    service: http://localhost:8000
  - service: http_status:404
```

### Step 5: DNS 指向 tunnel

```bash
cloudflared tunnel route dns TQark-web studyark.example.com
```

### Step 6: 跑起來

```bash
cloudflared tunnel run TQark-web
# 或用 systemd:
sudo cloudflared service install
sudo systemctl enable cloudflared
sudo systemctl start cloudflared
```

之後 https://studyark.example.com 就會連到家裡的 :8000。

---

## 🔄 更新流程

```bash
# 1. 進到部署的機器
cd /path/to/TQark-web

# 2. 拉新版本
git pull

# 3. 重新 build + 重啟(假設用 docker compose)
docker compose -f deploy/docker-compose.yml build
docker compose -f deploy/docker-compose.yml up -d

# 4. 看 logs 確認沒問題
docker compose -f deploy/docker-compose.yml logs -f backend
```

---

## 🔍 故障排除

### Backend 起不來?

```bash
# 看詳細錯誤
docker compose logs backend

# 常見原因:
# 1. cookies 檔案不存在 → 參考 cookie-maintenance.md
# 2. port 8000 被佔 → 換 port 或殺掉佔用的 process
# 3. Playwright 沒裝 chromium → 進 container 跑 playwright install
```

### 搜尋回空列表?

```bash
# 1. 確認 StudyArk 可達
curl -I https://www.studyark.org/

# 2. 確認 cookies 沒過期(參考 cookie-maintenance.md)

# 3. 手動開 Playwright 看頁面
docker compose exec backend python -c "
import asyncio
from app.services.studyark_scraper import test_login
asyncio.run(test_login())
"
```

### 下載 500?

```bash
# 1. 看 logs
docker compose logs backend | tail -50

# 2. 該 PDF 可能太大(> 20MB)
#    解法:改 MAX_PDF_SIZE_MB env

# 3. StudyArk 該份試卷可能暫時無法下載
#    重試看看,或手動到 StudyArk 確認
```

---

## 📋 Checklist 上線前

- [ ] cookies 已準備好且未過期
- [ ] `.env` 已填好所有必要設定
- [ ] `/health` 回 200
- [ ] 試一輪搜尋 + 下載流程
- [ ] Rate limit 測試(連點 20 下應該被擋)
- [ ] 404 處理(故意打不存在的 exam_id)
- [ ] About 頁 / 免責聲明都寫好
- [ ] Cloudflare Tunnel 跑起來
- [ ] HTTPS 憑證 OK(Cloudflare 自動)
- [ ] 監控有接(UptimeRobot 之類)