# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased] — Design Phase (3rd Revision)

### Changed
- **2026-07-18 17:00** 從「公開服務 + AdSense」改為「**私有 invite-only**」(法律風險降低)
- **2026-07-18 17:00** Hosting 從「Cloudflare Tunnel」改為「**家裡主機 + DDNS + Caddy**」
- **2026-07-18 17:00** 對外 port 改為 **8443**(避免常見 80/8080)
- **2026-07-18 17:00** Reverse proxy 改為 **Caddy 2.7**(自動 HTTPS)
- **2026-07-18 17:00** 加 **Google OAuth** 認證(取代原本的「不登入」設計)
- **2026-07-18 17:00** 加 **Admin approval gate** + audit log + user management
- **2026-07-18 17:00** 移除 AdSense / 公開流量設計

### Added
- `docs/google-oauth-setup.md` — Google Cloud Console 設定 step-by-step
- `docs/user-management.md` — Admin / User SOP + DB schema
- `docs/security-model.md` — Threat model + 事故回應 SOP
- PROJECT_PLAN.md — 3rd revision(包含 DB schema、user model、開發里程碑)

### Removed
- ~~Cloudflare Tunnel 設定~~
- ~~AdSense / 廣告收入估算~~
- ~~公開服務的法律風險評估~~(改為私有 invite-only 評估)

---

## [Unreleased] — Design Phase (2nd Revision)

### Changed
- 2026-07-18: Project 名稱從 `studyark-web` 改為 `TQark-web`
- 2026-07-18: GitHub access 從 PAT 改為 SSH key(OpenClaw bot key 加到 apingchang 帳號)
- 2026-07-18: SSH config 設定用 alias(`github.com` 給 William、`gh-ameow` 給 Ann)避免 key 衝突

---

## [Unreleased] — Design Phase (1st Revision)

### Added
- Initial project scaffold
- PROJECT_PLAN.md — 完整設計書(第 1 版)
- README.md — 使用說明
- docs/architecture.md — 系統架構
- docs/deployment.md — 部屬指南(初版,後續大改)
- docs/cookie-maintenance.md — Cookie 維護指南
- CONTRIBUTING.md
- LICENSE (MIT)

---

## 狀態

**設計階段**,尚未開工 coding。等待 William 審核後進 Phase 1 MVP。

預計 Phase 1 完工時間:2-3 週
預計 Phase 2(上線)時間:3-5 天(設計確認後)