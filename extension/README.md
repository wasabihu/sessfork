# sessfork Companion Extension (Manifest V3)

An auditable, zero-dependency, bilingual (English / 简体中文) Chrome / Edge / Brave extension that lets you fork your logged-in sites' **HttpOnly Cookies + LocalStorage** to local `sessfork-mcp` with a single click — including multi-tab batch fork and per-domain session management.

## Why Use the Companion Extension?

- **Native Windows Chrome / Edge / Brave `v20` App-Bound Encryption (ABE) Support**: Starting with Chrome 127 on Windows, cookies on disk are locked while Chrome is running and encrypted via the SYSTEM-level Elevation Service. Rather than using malware-like process injection or elevation tricks, this extension asks Chrome itself (`chrome.cookies.getAll`) **only when you click "Fork This Site"** — zero antivirus false positives, zero file lock conflicts, zero need to close Chrome.
- **Zero Python Required on Windows**: Talks directly to the standalone Rust `sessfork-mcp` binary over Chrome Native Messaging (`com.wasabihu.sessfork`), atomically updating `~/.sessfork/state.json` and hot-reload snapshots in ~15 ms.
- **Deterministic Fixed Extension ID (`eipgnmlpcpmadpbignmifidnbjfmlphm`)**: Ships with an embedded public key in `manifest.json`, so `install.ps1` pre-registers the Native Messaging Host before you even load the extension — zero manual ID copying.
- **Optional Zero-Keychain-Prompt Mode on macOS / Linux**: Works identically on macOS and Linux if you prefer explicit per-click browser authorization over OS Keychain access.

## 30-Second Setup

1. **Run the 1-command installer** (installs `sessfork-mcp`, auto-installs `moli.exe`, adds PATH, pre-registers the fixed Native Messaging host, wires Antigravity / Cursor / Claude Code, and copies the extension folder path to your clipboard):
   - **Windows (PowerShell)**:
     ```powershell
     powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/wasabihu/sessfork/main/install.ps1 | iex"
     ```
   - **macOS / Linux**: install via `./install.sh --with-rust-mcp`, then run `sessfork-mcp register-host`.

2. **Load the unpacked extension in Chrome / Edge / Brave**:
   - Open `chrome://extensions` (or `edge://extensions` / `brave://extensions`).
   - Enable **Developer mode** (top right).
   - Click **Load unpacked**, press **`Ctrl+V`** + **`Enter`** (Windows `install.ps1` already copied `%USERPROFILE%\.sessfork\extension` to your clipboard!).

3. **Fork any logged-in site (already Connected!)**:
   - Open any site you're already logged into (e.g., `https://github.com`), click the **sessfork** icon — the badge is already green (**Connected**).
   - Click **Fork This Site** (or expand **Other Open Tabs** to batch-fork multiple selected tabs at once, and manage/remove synced domains right inside the popup).
   - Your headless AI agent (`sessfork-moli`) can immediately browse authenticated pages in the background without stealing window focus!

*(If you loaded a custom fork with a different Extension ID, the popup automatically displays the exact `sessfork-mcp register-host --extension-id <id>` command with a 1-click Copy button.)*

## Security Guarantee

- **Zero background scripts or telemetry**: No `service_worker`, no timers, no outbound network requests. Code runs **only** while the popup is open and you click a button.
- **Strict domain scoping**: The Rust `native-host` validates the target domain (rejecting overly broad public suffixes like `com.cn`, `co.uk`, `github.io`, `vercel.app`) and drops any cookie or origin outside the requested domain before writing `~/.sessfork/state.json`.
- **Never echoes secrets**: Native host responses return only counts (`cookiesSynced`, `originsSynced`), never raw cookie or token values.
