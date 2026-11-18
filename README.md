# sessfork

[![CI](https://github.com/wasabihu/sessfork/actions/workflows/ci.yml/badge.svg)](https://github.com/wasabihu/sessfork/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](pyproject.toml)

**Fork live Chrome auth sessions (Cookies + LocalStorage) into headless AI agent browsers — zero window focus theft, zero runtime dependencies.**

When AI coding agents ([Google Antigravity](https://antigravity.google/docs/commands/browser/), Claude Code, or Cursor) connect to your main Chrome instance to reuse your logged-in sessions, they hijack your active window, switch tabs, and interrupt your reading flow.

From first principles, **UI viewport focus** and **auth storage state** should never be coupled. `sessfork` decouples them in one command: it takes lock-free snapshots of both your live Chromium SQLite `Cookies` database and your LevelDB `Local Storage/leveldb` store, decrypts cookies in memory via native OS APIs, reconciles MVCC sequence numbers for SPA/JWT `localStorage` tokens, writes a strict `0600` [Playwright `storageState`](https://playwright.dev/docs/api/class-browser) file (`cookies` + `origins`), and wires a background headless [Playwright MCP](https://github.com/microsoft/playwright-mcp) server (`sessfork-browser`) so your agent works silently in the background.

## Highlights

- **Zero Runtime Dependencies**: Pure Python standard library (`>=3.9`). Uses macOS `CommonCrypto` (`/usr/lib/libSystem.B.dylib` via `ctypes`) for AES-128-CBC cookie decryption plus a built-in pure-Python [LevelDB SSTable/WAL](https://github.com/google/leveldb/blob/main/doc/log_format.md) + [Snappy](https://github.com/google/snappy/blob/main/format_description.txt) reader — works out of the box with macOS `/usr/bin/python3` or `uvx`.
- **Full Dual-Layer Auth (`Cookies` + `LocalStorage`)**:
  - **SQLite Cookies**: Handles [Chromium Cookie DB `meta.version >= 24`](https://gist.github.com/creachadair/937179894a24571ce9860e2475a2d2ec) (`SHA-256(host_key)` 32-byte plaintext prefix stripping), WAL/SHM live sidecars, and `SameSite` / `HttpOnly` fidelity.
  - **LevelDB LocalStorage (`origins`)**: Parses [Chromium's `_<origin>\x00` encoding-prefixed (`0x00` UTF-16LE / `0x01` Latin-1) records](https://www.cclsolutionsgroup.com/post/chromium-session-storage-and-local-storage) across `.ldb` SSTables and `.log` Write-Ahead Logs, honoring `kTypeDeletion` tombstones (`localStorage.removeItem`) so logged-out JWTs are never resurrected.
- **One-Command Closed Loop**: Fork domains, incrementally merge `~/.sessfork/state.json`, resolve absolute `npx` paths (avoiding [macOS GUI `$PATH` launch failures](https://discuss.ai.google.dev/t/bug-mcp-servers-crash-with-executable-file-not-found-in-path-when-antigravity-is-launched-via-macos-gui/138495)), and install the Agent `SKILL.md` in one shot.

## Quickstart

### 1. Install

```bash
# Option A: Install globally via uv directly from GitHub (recommended)
uv tool install git+https://github.com/wasabihu/sessfork.git

# Option B: Clone and install editable (or run directly with system Python)
git clone https://github.com/wasabihu/sessfork.git
cd sessfork
uv tool install -e .
# Zero-install alias from repo root:
# alias sessfork="PYTHONPATH=\"$PWD/src\" python3 -m sessfork"
```

### 2. One-Command Setup & Session Fork

```bash
# Install global Antigravity MCP server + Agent SKILL.md, and fork github.com & linear.app:
sessfork --setup github.com linear.app
```

*(On first run with cookies, macOS will prompt to allow reading `"Chrome Safe Storage"` from Keychain. For pure LocalStorage JWT sites, pass `--no-cookies` for zero Keychain prompts.)*

### 3. One-Sentence Usage Inside Your Agent

Once `--setup` has installed `~/.gemini/skills/sessfork/SKILL.md` ([Antigravity Agent Skills](https://antigravity.google/docs/skills/)) and `~/.gemini/config/mcp_config.json` ([Antigravity MCP Settings](https://antigravity.google/docs/settings/)), just tell your agent in plain language:

> *"用 sessfork 把我 Chrome 里的 linear.app 登录态分叉过来，在后台无头浏览器里帮我检查一下..."*

## CLI Reference

```bash
sessfork github.com linear.app          # Fork both Cookies + LocalStorage & ensure MCP wired
sessfork app.spa.io --no-cookies        # Fork LevelDB LocalStorage JWTs only (no Keychain prompt)
sessfork github.com --no-local-storage  # Fork SQLite Cookies only
sessfork x.com --browser brave          # Fork from Brave (supports: chrome, canary, brave, edge, arc)
sessfork internal.corp --target all     # Wire Antigravity + Claude Code + Cursor simultaneously
sessfork --status                       # Inspect forked Cookie domains & LocalStorage origins (redacted)
sessfork --clear linear.app             # Remove forked Cookies & LocalStorage for linear.app
```

## Architecture

```text
Live Chrome (Foreground, 100% Yours)
  ├── Cookies + Cookies-wal (SQLite)
  └── Local Storage/leveldb/*.ldb + *.log (LevelDB + Snappy)
        │  1. Lock-free copy to 0700 ephemeral temp dirs
        ▼
  sessfork (Zero-Dep Python >= 3.9)
        ├── crypto.py        macOS Keychain + CommonCrypto AES-128-CBC + v24 SHA256 strip
        ├── localstorage.py  Pure-Python LevelDB SSTable/WAL + Snappy + MVCC tombstone resolver
        ├── chrome.py        Active profile auto-discovery + dual-layer session extractor
        ├── state.py         Atomic 0600 ~/.sessfork/state.json (cookies + origins) upsert
        └── mcp_setup.py     Idempotent MCP & SKILL.md wiring (Antigravity / Claude / Cursor)
        │  2. Loaded via --storage-state
        ▼
  sessfork-browser (@playwright/mcp --headless)  <── Driven silently by AI Agent
```

## Security Design

- **Domain Allowlist by Default**: Only the domains/origins you explicitly name are extracted.
- **Strict File Permissions**: `~/.sessfork/` is `0700` and `~/.sessfork/state.json` is atomically created with `0600` permissions before a single byte is written.
- **Redacted Diagnostics**: `sessfork --status`, `--json`, and `SKILL.md` never print raw cookie or LocalStorage values to stdout or agent prompt context.
- **Responsible Use**: `sessfork` operates strictly within your signed-in OS user account and relies on native OS keyring authorization (macOS Keychain / Linux Secret Service) — it never bypasses OS access controls or sandboxes. Use it only on devices and accounts you own or are explicitly authorized to test, and always comply with target websites' Terms of Service.

## Development & Tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## License

MIT
