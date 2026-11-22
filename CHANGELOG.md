# Changelog

All notable changes to this project are documented here. This project follows [Semantic Versioning](https://semver.org/).

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

[0.1.3]: https://github.com/wasabihu/sessfork/releases/tag/v0.1.3
[0.1.2]: https://github.com/wasabihu/sessfork/releases/tag/v0.1.2
[0.1.1]: https://github.com/wasabihu/sessfork/releases/tag/v0.1.1
[0.1.0]: https://github.com/wasabihu/sessfork/releases/tag/v0.1.0
