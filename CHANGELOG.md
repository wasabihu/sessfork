# Changelog

All notable changes to this project are documented here. This project follows [Semantic Versioning](https://semver.org/).

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

[0.1.1]: https://github.com/wasabihu/sessfork/releases/tag/v0.1.1
[0.1.0]: https://github.com/wasabihu/sessfork/releases/tag/v0.1.0
