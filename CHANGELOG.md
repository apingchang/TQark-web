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

## [Unreleased] — Archive OCR Improvements (2026-07-21)

### Added
- **County-aware folder structure** (2026-07-21 18:30)
  - Each PDF: `<county>/<segment>/<grade>/<subject>/<filetype>/`
  - Unknown county: `其他X/` (3-char width)
  - Filename: `<county>_<year>_<exam>_<fileid>_<school>_<version>.pdf`
- **PDF title extraction pipeline** (`app/scraper/pdf_title.py`)
  - pdftotext first (fast path for text-based PDFs)
  - PyMuPDF + tesseract OCR fallback (auto-rotates page rotation)
  - `chi_tra` only (no +eng interference)
  - PSM 6 (uniform block, stable for rotated pages)
- **School/county statistics** (`app/scraper/school_stats.py`)
  - 22 Taiwan counties + simplified aliases
  - 桃園縣 → 桃園市 normalization
  - school_stats.json tracks per-fileid county + school
- **Multi-account archive** (`scripts/archive_multi_account.py`)
  - 4 accounts rotation, auto-switch on rate limit
  - Critical bug fix: rebind StudyArkRateLimit after importlib.reload
- **Migration tools** (`scripts/`)
  - `migrate_to_county_structure.py` — old → new folder layout
  - `remigrate_other_x.py` — re-OCR 其他X/ PDFs to recover county
  - `build_school_stats.py` — rebuild school/county index
  - `manual_county_overrides.py` — fallback for OCR-unrecognizable
- **CLI tools**
  - `tqark-archive-status` — cumulative progress + account status
  - `tqark-pdf-count` — PDF stats by level/grade
  - `tqark-schools` — school/county distribution

### Results (2026-07-21 21:42)
- 99 PDFs total across 9 counties
- County identification rate: ~82% (rest are 學力檢測 or no county in title)
- Archive speed: ~120 fileids/day (4 accounts)
- Estimated completion: ~168 days for 20,158 PDFs

### Known Limitations (Future Work)
- **直書中文標題 OCR** — tesseract 對「直書 + 注音 + 圖片型」PDF 完全失效
  - 影響 19 個 其他X/ PDF
  - Future: 逐字切割 + 90度旋轉 + tesseract (per William 建議, 2026-07-21 21:46)
  - Priority: 低 (不影響功能,只影響分類完美度)
- **Web UI county filter** — 從 dropdown 選 county 看所有 PDF
  - school_stats.json 已經是 county-aware,純前端工作
- **county 字典建表** — 用 Wikipedia API 查 county-school 對照
  - 對其他X/ 內的 filename 短名自動查表補 county

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