---
name: sessfork
description: Fork live Chrome login sessions (both SQLite HttpOnly Cookies and LevelDB LocalStorage JWT/SPA state) into a background headless Playwright MCP browser (`sessfork-browser`) so you can browse authenticated sites without stealing or interrupting the user's foreground Chrome window focus.
---

# sessfork — Zero-Focus-Theft Browser Session Forking

Use this skill whenever a task requires reading, testing, or interacting with a website where the user is already signed in on their local browser (Chrome, Brave, Edge, Arc, or Canary), **without hijacking or stealing focus from their active browser window**.

## Core Principle

Never attach to or take over the user's foreground Chrome window just to reuse auth state. Instead:
1. Fork the target domain's live **Cookies + LocalStorage (`origins`)** into `~/.sessfork/state.json` (`0600`).
2. Drive the background headless `sessfork-browser` MCP server (`@playwright/mcp --headless`).

## One-Step Workflow

### 1. Fork or Refresh Target Domain Auth State

Run `sessfork` with only the domain(s) needed for the current task (by default, `sessfork` extracts both encrypted SQLite Cookies and LevelDB `LocalStorage` JWT/SPA keys in a single lock-free pass, and incrementally merges into `~/.sessfork/state.json`):

```bash
sessfork <domain1> [domain2 ...]
```

Examples:
```bash
sessfork github.com linear.app
sessfork app.supabase.com --no-cookies    # Pure JWT/SPA LocalStorage only (skips Keychain)
sessfork x.com --browser brave
```

If `sessfork` is not yet on global `$PATH`, run via `uvx` or from a local source checkout:
```bash
uvx --from git+https://github.com/wasabihu/sessfork.git sessfork <domain>
# Or from a local repo root: PYTHONPATH=src python3 -m sessfork <domain>
```

### 2. Operate Silently in Background Headless Browser

Use the `sessfork-browser` MCP tools (`browser_navigate`, `browser_snapshot`, `browser_click`, `browser_type`, `browser_take_screenshot`) to execute the web workflow in the background.

- Note: Chromium batches `LocalStorage` flushes to disk every ~5 seconds; if the user *just* signed in a second ago and a token is missing, wait 5 seconds and re-run `sessfork <domain>`.
- If `sessfork-browser` was just added to `mcp_config.json` for the first time, ask the user to reload MCP servers once (or run `/mcp` in CLI).

## Security Guardrails

1. **Least Privilege**: Always pass explicit `<domain>` names (never pass `--all-domains` unless the user explicitly asks for it).
2. **Zero Secret Leakage**: NEVER run `cat`, `head`, or `view_file` on `~/.sessfork/state.json`, and NEVER echo raw cookie or LocalStorage token values into chat or logs. Use `sessfork --status` to inspect active counts safely.
