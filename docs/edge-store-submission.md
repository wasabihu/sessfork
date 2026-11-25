# Microsoft Edge Add-ons (\$0 Free) Submission Kit — `sessfork`

A copy-paste-ready checklist and metadata kit for publishing the **sessfork Companion Extension** to the [Microsoft Edge Add-ons Partner Center](https://partner.microsoft.com/en-us/dashboard/microsoftedge/overview) (free developer registration, zero fee).

---

## 1. Local Artifacts to Upload (`dist/edge-store/`)

Regenerate anytime from repo root:

```bash
python3 scripts/package_edge_store.py
```

| Partner Center Field | File Path | Specification |
| :--- | :--- | :--- |
| **Packages** (ZIP upload) | `dist/edge-store/sessfork-edge-store-0.2.1.zip` | MV3 ZIP with `"key"` field stripped (Partner Center rejects manifests containing `"key"`) |
| **Extension Logo** (Required) | `dist/edge-store/logo-300x300.png` | `300 × 300` PNG (1:1) |
| **Small Promotional Tile** | `dist/edge-store/tile-small-440x280.png` | `440 × 280` PNG |
| **Large Promotional Tile** | `dist/edge-store/tile-large-1400x560.png` | `1400 × 560` PNG |
| **Screenshots (1–2)** | `dist/edge-store/screenshot-en-1280x800.png` & `screenshot-zh-1280x800.png` | `1280 × 800` PNG (English & 简体中文) |

---

## 2. Store Properties & URLs

- **Category**: `Developer Tools`
- **Pricing**: Free
- **Privacy Policy URL**: `https://wasabihu.github.io/sessfork/privacy.html`
- **Website URL**: `https://wasabihu.github.io/sessfork/`
- **Support / Contact URL**: `https://github.com/wasabihu/sessfork/issues`

---

## 3. Store Listing Copy — English (`en-US`)

- **Extension Name** *(comes from `manifest.json`)*: `sessfork Companion`
- **Short Description** *(≤132 chars)*:
  ```text
  One-click local sync of logged-in Cookies & LocalStorage to sessfork-mcp so headless AI agents browse without stealing window focus.
  ```
- **Detailed Description**:
  ```text
  sessfork Companion lets you fork the login session (HttpOnly Cookies + LocalStorage) of any site you explicitly choose directly into your local `sessfork-mcp` headless AI agent browser — without stealing your active browser window focus, without cloud uploads, and without closing Edge or Chrome.

  Key Features:
  • Zero-focus-theft AI browsing: Keep reading or coding in your main Edge window while your local AI coding agent (Antigravity, Claude Code, Cursor, or any MCP client) browses authenticated pages in the background.
  • Per-click explicit consent: Zero background scripts, zero timers, zero automatic scraping. Data is read only when you open the popup and click "Fork This Site" (or select specific open tabs to batch-fork).
  • 100% local & auditable: Communicates solely with your local open-source Rust `sessfork-mcp` binary over Edge Native Messaging (`com.wasabihu.sessfork`). Zero remote servers, zero analytics, zero telemetry.
  • Built-in session manager: Inspect per-domain cookie/key counts, remove individual domains in one click, or clear all cached local sessions safely.
  • Bilingual UI: Instant English / 简体中文 switcher with adaptive Light & Dark themes.

  Prerequisites:
  Install the free open-source local companion binary (`sessfork-mcp`) via PowerShell on Windows or Bash on macOS/Linux — see https://github.com/wasabihu/sessfork
  ```
- **Search Terms** *(up to 7)*: `mcp`, `ai-agent`, `cookies`, `localstorage`, `developer-tools`, `headless`, `sessfork`

---

## 4. Store Listing Copy — Simplified Chinese (`zh-CN`)

- **简短说明**：
  ```text
  一键将当前登录站点的 Cookie 与 LocalStorage 纯本地同步给 sessfork-mcp，让后台 AI 无头浏览器免抢焦点访问已登录页面。
  ```
- **详细说明**：
  ```text
  sessfork 伴生扩展可将您手动选中的已登录网站会话（含 HttpOnly Cookie 与 LocalStorage）一键同步至本机 `sessfork-mcp` 无头浏览器，让 AI 编程助手（Antigravity、Claude Code、Cursor 等 MCP 客户端）在后台静默访问已登录页面，完全不打断、不抢占您的前台 Edge 窗口焦点。

  核心特性：
  • 零抢焦点后台浏览：前台照常写代码看网页，AI Agent 在后台无头浏览器中复用您授权的站点登录态。
  • 逐次手动显式授权：无后台常驻脚本（无 service_worker）、无定时任务，仅在您点开弹窗点击「同步当前站点」或勾选多标签页批量同步时运行。
  • 100% 纯本地开源可审计：仅通过浏览器原生 Native Messaging（com.wasabihu.sessfork）与您本机安装的开源 Rust 程序通信，零云端服务器、零埋点遥测。
  • 内置已同步站点管理：随时查看每个站点的 Cookie / LocalStorage 条数，支持按域名一键移除或二次确认清空全部。
  • 中英双语与深浅色自适应界面。

  使用前提：
  需在本机安装开源本地端 `sessfork-mcp`（支持 Windows / macOS / Linux），安装说明见 https://github.com/wasabihu/sessfork
  ```

---

## 5. Mandatory Permission Justifications & Certification Notes (Paste into Partner Center)

### Single Purpose Statement
```text
Export cookies and localStorage strictly for the user-selected tab(s) upon explicit click in the popup, and pass them locally via Native Messaging to the user's local `sessfork-mcp` developer tool so local headless AI agents can reuse authenticated sessions without hijacking the user's browser window.
```

### Permission Justifications
- **`cookies`**:
  ```text
  Required to read cookies (including HttpOnly session cookies) strictly for the domain of the active or user-selected tab(s) when the user explicitly clicks "Fork This Site" or "Fork Selected". No cookies are ever read in the background or sent to any remote server.
  ```
- **`activeTab`**:
  ```text
  Required to inspect the current active tab's URL/hostname when the user opens the extension popup so the popup can display the active domain and sync it on explicit click.
  ```
- **`scripting`**:
  ```text
  Required to run an inline, local-only script (no remote code) inside the user-selected tab solely when the user clicks sync, in order to read `window.localStorage` key/value pairs for SPA/JWT authentication alongside cookies.
  ```
- **`nativeMessaging`**:
  ```text
  Required to transmit the user-selected domain's cookies and localStorage directly on-device to the user's locally installed open-source Rust host (`com.wasabihu.sessfork` / `sessfork-mcp`). This replaces insecure disk decryption workarounds on Windows Chrome/Edge App-Bound Encryption.
  ```
- **Host permissions (`http://*/*`, `https://*/*`)**:
  ```text
  Developers use `sessfork` with arbitrary self-hosted portals, SaaS dashboards, documentation sites, and local dev servers (`http://localhost:*`), which cannot be known in advance. Access is strictly triggered per-click by the user inside the popup and never runs automatically in the background.
  ```

### Notes for Certification (Reviewer Test Guide)
```text
This extension is a local-only developer companion for the MIT open-source CLI/MCP server `sessfork-mcp` (https://github.com/wasabihu/sessfork).

How to test:
1. Open the extension popup without the local native host installed: the badge clearly shows "Setup needed" and displays the one-time local registration command (`sessfork-mcp register-host --extension-id <id>`) with a Copy button.
2. Optional full E2E test with local native host:
   - On Windows PowerShell, install the open-source binary from https://github.com/wasabihu/sessfork/releases (or run `install.ps1`).
   - Run `sessfork-mcp register-host --extension-id <installed-edge-extension-id>`.
   - Re-open the popup on any http/https page: the badge turns green ("Connected").
   - Click "Fork This Site": cookies and localStorage for that site are written strictly to `%USERPROFILE%\.sessfork\state.json` on the local machine. Zero external network traffic is generated.
3. No login account or paid subscription is required to test the extension.
```

---

## 6. Live Submission Record & Post-Approval Sync

Submitted to Microsoft Edge Add-ons Partner Center on **2026-11-26** (`Status: In review`, Version `0.2.1`):

- **Extension Name**: `sessfork — Local Session Bridge`
- **Store ID**: `0RDCKDW6WFH7`
- **Permanent Edge Store CRX ID**: `gfjpmefbcbenccjlmakaalgnmkpbageo`
- **Partner Center Product ID**: `17aab622-e3db-489b-b355-50457ba9542b`
- **Privacy Policy URL**: `https://wasabihu.github.io/sessfork/privacy.html`

Once Microsoft Edge Add-ons approves the listing:

1. Add `gfjpmefbcbenccjlmakaalgnmkpbageo` alongside `DEFAULT_EXTENSION_ID` (`eipgnmlpcpmadpbignmifidnbjfmlphm`) in `rust/sessfork-mcp/src/native_host.rs` (`allowed_origins`) and `extension/popup.js` so `sessfork-mcp register-host` pre-authorizes **both** the Edge Add-ons Store build and the unpacked GitHub build.
2. Print the direct Edge Add-ons Store one-click install link (`https://microsoftedge.microsoft.com/addons/detail/gfjpmefbcbenccjlmakaalgnmkpbageo`) in `install.ps1` and `README.md` / `README.zh-CN.md`, and ship `v0.2.2`.
