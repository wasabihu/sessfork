# Changelog

All notable changes to this project are documented here. This project follows [Semantic Versioning](https://semver.org/).

## [0.2.1] - 2026-11-25

Real-machine Windows 10 QA bugfixes, zero-friction 1-command Windows installer (`install.ps1`), deterministic fixed Extension ID, and redesigned bilingual Companion Extension UI.

### Fixed
- **Windows `moli.exe` Auto-Discovery (`rust/sessfork-mcp/src/main.rs`)**: Added `%LOCALAPPDATA%\Moli\bin\moli.exe` (and `%LOCALAPPDATA%\Programs\Moli\bin\moli.exe`) ahead of legacy fallback paths in `find_moli_binary()`, matching `lexmount/moli`'s official `moli-installer.ps1` destination so `sessfork-mcp.exe` works out of the box with zero `--moli` flags.
- **Chinese Windows `register-host` Mojibake (`rust/sessfork-mcp/src/native_host.rs`)**: Captured `reg.exe` stdout/stderr via `.output()` instead of inheriting console handles so localized GBK output (`操作成功完成。`) never prints as garbled bytes in UTF-8 terminals.
- **`sessfork-mcp native-host --help` Blocking (`rust/sessfork-mcp/src/native_host.rs`)**: Handled `-h` / `--help` immediately with exit code `0` instead of blocking on stdin waiting for a Chrome Native Messaging frame.

### Added & Improved
- **Deterministic Fixed Extension ID (`eipgnmlpcpmadpbignmifidnbjfmlphm`) & Zero-Flag `register-host` (`extension/manifest.json`, `native_host.rs`)**: Embedded an RSA-2048 public key (`"key"`) and crisp vector-rendered brand icons (`extension/icons/{16,48,128}.png`) into the Manifest V3 Companion Extension, making `--extension-id` optional in `sessfork-mcp register-host`.
- **True 1-Command Windows Installer (`install.ps1`)**: Automatically detects or installs `moli.exe`, adds both `sessfork-mcp` and `moli` to User PATH, pre-registers the fixed Native Messaging host (`register-host`), auto-wires detected AI coding clients (Antigravity / Cursor / Claude Code), and copies `%USERPROFILE%\.sessfork\extension` to the Windows clipboard for instant `Ctrl+V` loading.
- **Redesigned Bilingual Companion Extension (`extension/popup.html`, `extension/popup.js`, `native_host.rs`)**: Minimalist Swiss UI with adaptive light/dark themes, auto-detected EN / 简体中文 switcher, per-domain `domainStats` (`cookies`, `httpOnlyCookies`, `origins`, `localStorageKeys`), collapsible multi-tab batch fork drawer, per-domain quick remove, 2-step safe Clear All, and progressive setup guidance.

## [0.2.0] - 2026-11-25

Native Windows x64 support (zero Python, zero Node.js, zero Tokio), built-in Chrome/Edge Native Messaging host, and auditable Manifest V3 Companion Extension solving Chrome 127+ App-Bound Encryption (`v20`) without process injection.

### Added
- **Native Windows CDP WebSocket Transport & Win32 Job Object (`rust/sessfork-mcp/src/ws_cdp.rs`, `cdp.rs`)**: Launches `moli.exe serve --host 127.0.0.1 --port 0` (kernel-assigned port read directly from child stderr, eliminating port TOCTOU) with `CREATE_NO_WINDOW` and a Win32 Job Object (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`) for guaranteed zero-orphan teardown, speaking pure-std RFC 6455 WebSocket (`34.2 ms` cold spawn, `0.086 ms/call` vs `23.2 ms` / `0.042 ms/call` on Unix pipe; `~784 KiB` stripped binary).
- **Built-in Native Messaging Host, Auto-Registrar & Zero-Python MCP Setup (`rust/sessfork-mcp/src/native_host.rs`, `main.rs`)**: Added `native-host` (auto-detected when launched by Chrome/Edge with `chrome-extension://` or `--parent-window=`), `register-host --extension-id <ID>` (Windows `HKCU` Registry via qualified `System32\reg.exe` + macOS/Linux JSON manifests with `\\?\` prefix stripping), and `setup --mcp-file <PATH>` atomic MCP config wiring.
- **Auditable Manifest V3 Companion Extension (`extension/`) & Windows Installer (`install.ps1`)**: ~200-line zero-dependency Chrome/Edge/Brave extension with default exact-host scoping, tab navigation origin verification, and per-click user consent so Chrome natively decrypts `v20` cookies without file locks or antivirus false positives; added PowerShell one-line installer (`install.ps1`) with TLS 1.2 enforcement and fail-closed SHA-256 verification.
- **Windows CI & Release Packaging (`.github/workflows/ci.yml`, `.github/workflows/release.yml`)**: Added `windows-latest` CI (Rust locked test/clippy/release + PowerShell AST syntax check) and automated `sessfork-mcp-windows-x64.zip` + `sessfork-extension.zip` Release assets with unified 4-manifest version checks.

## [0.1.6] - 2026-11-25

Auth-to-XERJ zero-dependency ingestion skill, Official MCP Registry manifest, and automated PyPI Trusted Publisher release pipeline.

### Added
- **Auth-to-XERJ Ingest Skill (`skills/xerj-auth-ingest/`)**: Added `SKILL.md` and zero-dependency stdlib cleaner `ingest_to_xerj.py` to convert authenticated HTML/Markdown/JSONL captures into UTF-8 Markdown with YAML frontmatter, built-in secret/JWT/Bearer/Cookie redaction (`<REDACTED>`), loopback-only XERJ URL validation, path-traversal guards, dry-run default, and safe `xerj autoindex` invocation, plus unit & isolated E2E tests (`tests/test_xerj_auth_ingest.py`) and XERJ ecosystem cross-links in `README.md` / `README.zh-CN.md`.
- **Official MCP Registry & PyPI Publishing (`server.json`, `.github/workflows/release.yml`)**: Added `server.json` (`io.github.wasabihu/sessfork`) and automated PyPI Trusted Publisher (`uv build` + OIDC) and Official MCP Registry publishing jobs on `v*` tags.

## [0.1.5] - 2026-11-25

Comprehensive security hardening release across the Python CLI, local Studio UI, installer/CI supply chain, and native Rust `sessfork-mcp` server, plus native Moli WebMCP bridge and structure-first Markdown extraction.

### Security
- **Cross-Origin LocalStorage Isolation & Authoritative Page URLs (`rust/sessfork-mcp`)**:
  - Moved `Page.addScriptToEvaluateOnNewDocument` LocalStorage hydration into an isolated CDP world (`worldName`) backed by a JS `Map` (eliminating `Object.prototype` lookup), and filtered hot-reload live sync strictly in Rust per tab origin via `Page.createIsolatedWorld` so an untrusted open tab never receives another origin's LocalStorage secrets.
  - Switched `Page URL` and tab origin tracking (`sync_current_page_meta`, `browser_extract_markdown`, `browser_webmcp_list/call`) to browser-authoritative CDP `Page.getFrameTree` and eliminated main-world `window.JSON.stringify` reliance.
  - Added frame origin attribution (`frameUrl`, `frameOrigin`, `isTopFrame`, `sameOriginWithTop`) to `browser_webmcp_list` and restricted implicit `browser_webmcp_call` (without explicit `frameId`) to same-origin frames.
  - Hardened `browser_take_screenshot` fail-closed (`0600`, `O_NOFOLLOW`, rejecting path traversal, hidden files like `.sessfork-state.json`, non-`.png` extensions, and symlinks), narrowed `browser_navigate` to `http://`, `https://`, and `about:blank`, hid filesystem existence oracles & sanitized JSON parse errors in `browser_set_storage_state`, fixed $O(N^2)$ CDP frame scanning + added 64 MiB frame cap and console/WebMCP memory bounds, and prevented `Drop` deadlock if a grandchild holds FD 4.
- **Fail-Closed Domain Allowlist & Credential Storage Hardening (`chrome.py`, `localstorage.py`, `state.py`, `crypto.py`, `cli.py`)**:
  - Blocked blank/whitespace domain arguments (`sessfork ""`, `sessfork --clear ""`) from silently degrading into `--all-domains` export or full state wipe; decoupled explicit `all_domains` from empty lists.
  - Fixed symlink guards to run **before** `.resolve()` across `state.json`, `playwright-output/.sessfork-state.json`, `keys.json`, lock files, Cookie DB sidecars (`-wal`/`-shm`/`-journal` checked before `sqlite3.connect`), and `Local Storage/leveldb` directories; enforced `O_NOFOLLOW` on snapshot file copies and eliminated `0755->0700` creation race and FD leak.
  - Enforced strict `SHA256(host_key)` verification on Chrome v24+ cookie decryption (preventing blind 32-byte stripping and broken stale-key self-healing), validated `profile` names against path traversal, expanded shared public suffix & generic second-level ccTLD guards, pruned logged-out cookies on domain refresh, fixed pre-1970 expired cookie resurrection, validated AES key/IV lengths before `ctypes` calls, and hardened LevelDB partial-block recovery.
- **Local Studio UI & Supply Chain Hardening (`ui_server.py`, `moli_bridge.py`, `mcp_setup.py`, `install.sh`, `docs/index.html`, `.github/workflows/*`)**:
  - Enforced strict loopback `Host` (exact bound port), `Origin`, `Sec-Fetch-Site`, anti-clickjacking headers (`X-Frame-Options: DENY`, `CSP: frame-ancestors 'none'`), and per-session authentication across all `GET` and `POST` Studio UI routes, with `0600` local HTML auto-POST launcher so tokens never appear in OS argv; fail-closed on empty/oversized (`413`) JSON bodies in `/api/clear` and validated `profile`/`browser` in `/api/fork`.
  - Bypassed `HTTP_PROXY`/`ALL_PROXY` during local `moli_bridge` loopback readiness probes and documented multi-user tradeoffs of the legacy TCP fallback.
  - Bundled canonical `SKILL.md` into wheel package data (`src/sessfork/data/SKILL.md`) with complete `--caps=storage` read-tool bans and atomic symlink-safe skill installation.
  - Pinned `install.sh` and `docs/index.html` downloads to release tags with `--proto '=https' --tlsv1.2`, single-member tar extraction + symlink checks, atomic `0700` dir / staging installs, strict `:port` validation in the web command builder, full 40-char Commit SHA Action pins, `cargo --locked`, and `RELEASE_TAG` validation in CI/Release workflows.

### Added
- **Native Moli WebMCP Bridge & Structure-First Markdown (`rust/sessfork-mcp`)**: Added `browser_webmcp_list`, `browser_webmcp_call` (with declarative manual-confirmation guard), and zero-layout `browser_extract_markdown`.
- **Cookie Snapshot & CHIPS Isolation (`chrome.py`, `cdp.rs`)**: Preferred `Network/Cookies` over stale root `Cookies`, excluded third-party CHIPS partitioned cookies by default with deterministic priority deduplication, added `vivaldi` & `chromium` browser mappings, and added process-group (`setpgid`/`killpg`) teardown on timeout.

## [0.1.4] - 2026-11-24

Native 605 KB Rust `sessfork-mcp` CDP pipe server, Linux arm64 support, cross-process `fcntl.flock` locking, local Studio UI, and Dual-Engine Moli routing.

## [0.1.3] - 2026-11-23

Fix macOS headless Chrome multi-origin `storageState` crashes by auto-pinning Playwright's `chrome-headless-shell`.

### Fixed
- **macOS Multi-Origin Headless Crash (`mcp_setup.py`)**: Added shallow Playwright cache discovery (`find_playwright_headless_shell()`) that selects the highest numeric `chromium_headless_shell-<rev>` binary for the host architecture (skipping symlinks and non-executable files) and automatically appends `--executable-path=<chrome-headless-shell>` in `build_playwright_mcp_entry()` on macOS. This avoids system Chrome's `--headless --isolated --storage-state` crash (`Mach rendezvous failed` / `Target page, context or browser has been closed`) when hydrating many LocalStorage origins. Supports `PLAYWRIGHT_BROWSERS_PATH` and `SESSFORK_BROWSER_EXECUTABLE_PATH` (including `system` / `none` to disable auto-pinning, or an explicit executable path on any OS).
- **MCP Diagnostics & Setup Guidance (`mcp_setup.py`, `cli.py`)**: Added `inspect_mcp_headless_shell()` and surfaced `playwright_headless_shell`, pinned/unpinned/stale MCP config status, and actionable macOS warnings in `sessfork --status` (text and `--json`) and `sessfork --setup`.

## [0.1.2] - 2026-11-19

Zero-restart MCP hot reload and one-shot domain refresh.

### Added
- **Zero-Restart Hot Reload (`state.py`, `SKILL.md`)**: Atomically mirrors `state.json` (`0600`) inside Playwright MCP's `0700` `--output-dir` (`~/.sessfork/playwright-output/.sessfork-state.json`) so running `sessfork-browser` instances can hot-reload updated sessions in-memory via `browser_set_storage_state` without restarting MCP.
- **One-Shot Refresh (`cli.py`, `state.py`)**: Added `-r` / `--refresh` to re-fork all domains currently saved in `state.json` (plus any newly specified domains) in a single command, powered by `list_state_domains()` subdomain collapsing.

## [0.1.1] - 2026-11-19

Security hardening release.

### Security
- **Linux In-Memory AES (`crypto.py`)**: Replaced `openssl enc -K ...` subprocess invocation with direct `ctypes` OpenSSL `libcrypto` (`EVP_*`) calls so derived keys never appear in `/proc/<pid>/cmdline`.
- **Pinned MCP Package (`mcp_setup.py`)**: Pinned `@playwright/mcp@0.0.85` instead of floating `@latest` (overridable via `SESSFORK_PLAYWRIGHT_MCP_PKG`), explicitly enforced `0700` permissions on `~/.sessfork` and `playwright-output/`, and made MCP config writes atomic with `0600` mode for new files.
- **Domain Allowlist & Public Suffix Guards (`chrome.py`, `docs/index.html`)**: Validated hostname characters, rejected bare single-label TLDs (except `localhost`) and multi-tenant public suffixes (`github.io`, `vercel.app`, `co.uk`, etc.), and hardened the interactive web generator input normalizer.
- **Symlink Guards (`chrome.py`, `localstorage.py`)**: Refused symlinked Cookie SQLite databases and skipped symlinked WAL/SHM sidecars and LevelDB files during snapshot extraction.
- **Installer Hardening (`install.sh`)**: Explicitly enforced `0700` on `~/.sessfork`, fixed GNU/BSD `mktemp` template compatibility, and pinned default ref to `v0.1.1`.

## [0.1.0] - 2026-11-18

First public release.

### Added
- Lock-free fork of Chromium SQLite `Cookies` (incl. Chrome 130+ `meta.version >= 24`) and LevelDB `Local Storage` into a `0600` Playwright `storageState` file.
- Pure-Python LevelDB SSTable/WAL + Snappy reader with MVCC tombstone handling.
- Auto-wiring of the headless `sessfork-browser` MCP server (`@playwright/mcp --headless --isolated`) for Antigravity, Claude Code and Cursor (`--target auto`), plus `SKILL.md` installation. Snapshots/screenshots are written to `~/.sessfork/playwright-output/` instead of the agent's current project directory.
- One-line installer (`install.sh`) and `uvx` zero-install usage.
- `--refresh-key`, `--purge-keys`, and opt-in `--cache-key` for the derived Safe Storage key (cache is **off** by default).
- Bilingual documentation (English / 简体中文), SVG diagrams and an interactive guide on GitHub Pages.
- `SECURITY.md` with threat model and private reporting channel.

[0.2.1]: https://github.com/wasabihu/sessfork/releases/tag/v0.2.1
[0.2.0]: https://github.com/wasabihu/sessfork/releases/tag/v0.2.0
[0.1.6]: https://github.com/wasabihu/sessfork/releases/tag/v0.1.6
[0.1.5]: https://github.com/wasabihu/sessfork/releases/tag/v0.1.5
[0.1.4]: https://github.com/wasabihu/sessfork/releases/tag/v0.1.4
[0.1.3]: https://github.com/wasabihu/sessfork/releases/tag/v0.1.3
[0.1.2]: https://github.com/wasabihu/sessfork/releases/tag/v0.1.2
[0.1.1]: https://github.com/wasabihu/sessfork/releases/tag/v0.1.1
[0.1.0]: https://github.com/wasabihu/sessfork/releases/tag/v0.1.0
