# sessfork Companion Extension (Manifest V3)

An auditable, ~200-line, zero-dependency Chrome / Edge / Brave extension that lets you fork your current logged-in site's **HttpOnly Cookies + LocalStorage** to local `sessfork-mcp` with a single click.

## Why Use the Companion Extension?

- **Native Windows Chrome / Edge / Brave `v20` App-Bound Encryption (ABE) Support**: Starting with Chrome 127 on Windows, cookies on disk are locked while Chrome is running and encrypted via the SYSTEM-level Elevation Service. Rather than using malware-like process injection or elevation tricks, this extension asks Chrome itself (`chrome.cookies.getAll`) **only when you click "Fork This Site"** — zero antivirus false positives, zero file lock conflicts, zero need to close Chrome.
- **Zero Python Required on Windows**: Talks directly to the standalone Rust `sessfork-mcp` binary over Chrome Native Messaging (`com.wasabihu.sessfork`), atomically updating `~/.sessfork/state.json` and hot-reload snapshots in ~15 ms.
- **Optional Zero-Keychain-Prompt Mode on macOS / Linux**: Works identically on macOS and Linux if you prefer explicit per-click browser authorization over OS Keychain access.

## 60-Second Setup

1. **Install `sessfork-mcp` and `moli`**:
   - **Windows (PowerShell)**:
     ```powershell
     powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/wasabihu/sessfork/main/install.ps1 | iex"
     powershell -ExecutionPolicy ByPass -c "irm https://github.com/lexmount/moli/releases/latest/download/moli-installer.ps1 | iex"
     ```
   - **macOS / Linux**: install via `./install.sh --engine moli` or download from GitHub Releases.

2. **Load the unpacked extension in Chrome / Edge / Brave**:
   - Open `chrome://extensions` (or `edge://extensions` / `brave://extensions`).
   - Enable **Developer mode** (top right).
   - Click **Load unpacked** and select this `extension/` directory (or the unzipped `sessfork-extension` folder from GitHub Releases).

3. **Register the local Native Messaging Host (one-time)**:
   - Click the **sessfork** extension icon in your toolbar — it automatically displays your extension ID and the exact registration command.
   - Copy and run it in your terminal:
     ```bash
     sessfork-mcp register-host --extension-id <your-32-char-id>
     ```
   - Click the extension icon again: the badge turns green (**Connected**).

4. **Fork any logged-in site**:
   - Open any site you're already logged into (e.g., `https://github.com`), click the **sessfork** icon → **Fork This Site**.
   - Your headless AI agent (`sessfork-moli`) can immediately browse authenticated pages in the background without stealing window focus!

## Security Guarantee

- **Zero background scripts or telemetry**: No `service_worker`, no timers, no outbound network requests. Code runs **only** while the popup is open and you click a button.
- **Strict domain scoping**: The Rust `native-host` validates the target domain (rejecting overly broad public suffixes like `com.cn`, `co.uk`, `github.io`, `vercel.app`) and drops any cookie or origin outside the requested domain before writing `~/.sessfork/state.json`.
- **Never echoes secrets**: Native host responses return only counts (`cookiesSynced`, `originsSynced`), never raw cookie or token values.
