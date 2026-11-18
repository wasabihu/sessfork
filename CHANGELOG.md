# Changelog

All notable changes to this project are documented here. This project follows [Semantic Versioning](https://semver.org/).

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

[0.1.0]: https://github.com/wasabihu/sessfork/releases/tag/v0.1.0
