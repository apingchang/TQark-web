# Security Model

> Threat model + 防護設計,給之後 Phase 1 實作跟 review 用。

---

## 🎯 我們在防什麼?

### Threat model(誰會攻擊我們,怎麼攻)

| 威脅 | 機率 | 影響 | 防護 |
|------|------|------|------|
| **陌生人亂試 access** | 🔴 高 | 🟢 低(進不來) | Admin approval gate |
| **Approved user 抓過頭 StudyArk ban IP** | 🟡 中 | 🔴 高(整站停擺) | Rate limit + cache + 監控 |
| **版權方(學校/老師)寄信** | 🟡 中 | 🟡 中 | Disclaimer + 移除機制 + 不對外 |
| **User 帳號被盜用** | 🟢 低 | 🟡 中 | 2FA(Google 端)+ Block 功能 |
| **Admin 帳號被盜** | 🟢 低 | 🔴 高 | 2FA + audit log + 緊急 SOP |
| **SQL injection** | 🟢 低 | 🔴 高 | Pydantic + ORM(避免字串拼接) |
| **XSS** | 🟢 低 | 🟡 中 | Content Security Policy + template escape |
| **CSRF** | 🟢 低 | 🟡 中 | SameSite cookie + CSRF token |
| **DDoS** | 🟢 低(50+ 人規模) | 🟡 中 | Caddy 內建 rate limit + fail2ban |
| **Secrets 洩漏到 git** | 🟡 中(意外) | 🔴 高 | .gitignore + pre-commit hook + secret rotation |

---

## 🛡 防護設計

### 1. 認證 / 授權

| 機制 | 做法 |
|------|------|
| **身份驗證** | Google OAuth 2.0(我們不存密碼) |
| **Session 管理** | JWT(httpOnly cookie,SameSite=Strict)+ 24 小時過期 |
| **Admin 識別** | DB `role='admin'` + 首次從 `ADMIN_EMAILS` env var 同步 |
| **API 認證** | 每個 request 帶 JWT cookie,後端 middleware 驗證 |

### 2. 輸入驗證

| 機制 | 做法 |
|------|------|
| **Pydantic schemas** | 所有 API 輸入用 Pydantic 驗證(type + range + regex) |
| **SQL injection 防護** | 全部用 SQLAlchemy ORM,禁用 raw SQL |
| **Path traversal** | exam_id 只允許 alphanumeric + dash,不接受 `../` |
| **HTML 渲染** | Jinja2 auto-escape(預設開)+ CSP header |
| **File upload** | Phase 1 沒 upload 功能,之後加要嚴格檢查類型 + size |

### 3. Rate Limiting

| 對象 | 限制 |
|------|------|
| **未登入** | 60 req/hour per IP(防止枚舉) |
| **一般 User** | 30 req/min per user,500 req/day per user |
| **Admin** | 不限制(但 audit log 一筆不漏) |
| **Search 特別嚴** | 10 req/min per user(避免打爆 StudyArk) |
| **Download 特別嚴** | 5 req/min per user,30 req/hour per user |
| **實作** | slowapi(FastAPI 的 rate limit middleware) |

### 4. Audit Log

- 所有重要操作寫進 `audit_log` table
- 包括:登入、申請、approve、block、搜尋、下載、admin 操作
- IP 只存 `SHA256(ip)[:16]`(去識別化但可關聯)
- Retention: 永久
- Admin 可以在 `/admin/audit` 看 + 匯出 CSV

### 5. Secrets 管理

| Secret | 怎麼存 |
|--------|--------|
| Google Client ID | `.env`(`GOOGLE_CLIENT_ID`) |
| Google Client Secret | `.env`(`GOOGLE_CLIENT_SECRET`) |
| JWT signing key | `.env`(`JWT_SECRET`,`openssl rand -hex 32` 產生) |
| StudyArk cookies | `data/cookies/studyark_cookies.json`,`chmod 600` |
| Database | `data/db/app.db`,`chmod 600` |

**絕對不要**:
- Commit `.env` 到 git(.gitignore 排除)
- Hardcode secret 在程式碼
- 把 secret 寫到 log
- 把 secret 分享到 Slack/Email

**定期 rotate**:
- JWT secret:每 6 個月(會強制所有 user 重新登入)
- Google Client Secret:每 6 個月
- StudyArk cookies:看過期時間,過期前手動更新

### 6. Network 安全

| 機制 | 做法 |
|------|------|
| **HTTPS only** | Caddy 自動 cert + HSTS header(`max-age=31536000`) |
| **Reverse proxy** | Caddy 在前面擋,FastAPI 只 listen `127.0.0.1:8000` |
| **不暴露管理介面** | `/admin/*` 跟一般 `/api/*` 一樣,但需要 admin role |
| **不暴露內部 port** | 對外只有 8443,其他 port 都在 Linux firewall 後面 |
| **fail2ban** | 自動 ban 太多失敗 login 的 IP(可選加) |

### 7. 資料保護

| 資料 | 保護 |
|------|------|
| User email / name | Google 給的,我們只 cache 不分享 |
| Search 記錄 | DB 存,audit log 也存(防 user 不認帳) |
| Download 記錄 | 同上 |
| PDF cache | 本地檔案,不在網路流傳(只透過 HTTPS) |
| Backup | 加密備份(`gpg` 或 `age`),密碼存密碼管理器 |

---

## 🚨 事故回應 SOP

### 事故 A:StudyArk cookies 過期 / StudyArk ban 我們 IP

**症狀**:`/health` 回 `cookie_status: expired`,或所有 search 回空

**SOP**:
1. SSH 進 server,看 `backend.log`
2. 重新跑 cookie 取得 script(詳見 `cookie-maintenance.md`)
3. 重啟 `tqark-web` service
4. 如果 StudyArk ban IP:聯絡 StudyArk(沒聯絡方式就只能換 DDNS / VPN)

### 事故 B:某 user 抓過頭,造成 StudyArk 懷疑

**症狀**:StudyArk 開始要 CAPTCHA、登入失敗

**SOP**:
1. 立即停掉對應 user 的下載權限(block)
2. 降低 rate limit
3. 增加 cache(把常用試卷 pre-download)
4. 必要時聯絡 StudyArk 解釋

### 事故 C:Admin 帳號被盜

**症狀**:audit log 看到不是你做的 admin 操作

**SOP**:
1. 立即到 Google 重設密碼 + 撤銷所有 sessions
2. SSH 進 server,把被盜 admin 的 `role='user'` 改掉:
   ```bash
   sqlite3 /home/aping/TQark-web/data/db/app.db \
     "UPDATE users SET role='user' WHERE email='compromised@email';"
   ```
3. 檢查 audit log,把所有可疑操作 rollback(block 掉的可疑 user)
4. 換所有 secrets(JWT secret、Google Client Secret)
5. 之後看要不要強制所有 user 重新登入(換 JWT secret 自動)

### 事故 D:DB 損壞 / 資料遺失

**症狀**:user 抱怨登入失敗、找不到資料

**SOP**:
1. **不要慌**,先 backup 現有 DB(`cp app.db app.db.broken`)
2. 從最近的 backup 還原
3. 通知 user 可能某些資料(申請訊息、audit log)不見了
4. 之後一定設 backup cron!

### 事故 E:版權方(學校/老師)來信要求下架

**症狀**:收到 email 或律師信,說我們侵權

**SOP**:
1. **不要立刻回**,先看完整內容
2. 確認對方身份(是不是真的有版權)
3. **如果對方真的有版權**:
   - 把對應試卷從 cache 刪掉
   - 在 metadata DB 加黑名單(之後 search 排除)
   - 回信告知已處理
4. **如果對方沒有版權或要求不合理**:
   - 可以禮貌回信,說明服務性質
   - 必要時找律師諮詢
5. **不管哪種,audit log 完整保留**(證據)

### 事故 F:GitHub repo 被搞(被 fork 加惡意 code、PR 攻擊)

**症狀**:GitHub 上看到不預期的 commit 或 PR

**SOP**:
1. 因為是 private repo(預設),應該不太會發生
2. 如果發生:用 `git revert` 還原 + 改 SSH key
3. 開 branch protection(防止直接 push main)

---

## 📋 定期維護清單

### 每天
- [ ] 看 `backend.log` 有沒有 error
- [ ] 看 `/health` endpoint

### 每週
- [ ] 看 audit log(找可疑 user 行為)
- [ ] 看 Caddy log(找可疑 request pattern)

### 每月
- [ ] 看 StudyArk cookies 還有多久過期
- [ ] 看 disk usage(避免 PDF cache 把磁碟塞爆)
- [ ] backup DB 到外接硬碟
- [ ] `apt update && apt upgrade`(更新 Caddy / 其他套件)

### 每季
- [ ] 換 JWT secret(會強制 user 重新登入,記得先通知)
- [ ] 換 Google Client Secret
- [ ] `sudo caddy trust` 確認 cert 還有效

### 每年
- [ ] 完整 backup + 還原演練(確認 backup 可用)
- [ ] security model review(這份文件 + 看看有沒有新威脅)

---

## 🔍 安全檢查工具(推薦裝)

### Pre-commit hook(防止 commit secret 到 git)

安裝 `detect-secrets`:
```bash
pip install detect-secrets
cd /home/aping/TQark-web
detect-secrets scan > .secrets.baseline
echo "detect-secrets-hook" >> .git/hooks/pre-commit
```

之後 commit 會自動掃描,有 secret 就擋下來。

### Dependency vulnerability check

```bash
# 定期跑(可加到 cron 每週跑)
pip install safety
safety check -r /home/aping/TQark-web/backend/requirements.txt
```

### Caddy log 監控(找攻擊 pattern)

```bash
# 找可疑 user agent(curl、python-requests 等)
grep -E "curl|python|wget" /var/log/caddy/tqark-access.log | head

# 找 SQL injection 嘗試
grep -iE "union|select|drop|insert" /var/log/caddy/tqark-access.log | head
```

---

## 📚 參考

- OWASP Top 10:https://owasp.org/www-project-top-ten/
- FastAPI Security:https://fastapi.tiangolo.com/tutorial/security/
- Google OAuth Security:https://developers.google.com/identity/protocols/oauth2/security-best-practices
- Caddy Security:https://caddyserver.com/docs/caddyfile/options