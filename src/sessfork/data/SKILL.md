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
sessfork --refresh                        # Re-fork all domains currently saved in state.json
```

Examples:
```bash
sessfork github.com linear.app
sessfork app.supabase.com --no-cookies    # Pure JWT/SPA LocalStorage only (skips Keychain)
sessfork x.com --browser brave
sessfork --refresh                        # Refresh all existing saved domains in one pass
```

If `sessfork` is not yet on global `$PATH`, run via `uvx` or from a local source checkout:
```bash
uvx --from git+https://github.com/wasabihu/sessfork.git@v0.2.1 sessfork <domain>
# Or from a local repo root: PYTHONPATH=src python3 -m sessfork <domain>
```

### 2. Zero-Restart Hot Reload into Running `sessfork-browser`

After running `sessfork <domain>` or `sessfork --refresh` inside an already-running session, hot-reload the updated auth state straight into `sessfork-browser`'s live memory (~5ms) without asking the user to restart MCP:

1. Call `sessfork-browser`'s `browser_set_storage_state` tool with `filename` set to the `Hot-reload path` reported by `sessfork --status` (i.e. `<real_home>/.sessfork/playwright-output/.sessfork-state.json`). This loads directly from disk inside Playwright MCP's `--output-dir` sandbox without exposing any secret values.
2. Only ask the user to reload/toggle `sessfork-browser` once if `sessfork-browser` was just added to `mcp_config.json` for the first time (or if an older `sessfork-browser` process started before `--output-dir` was configured returns `outside allowed roots`).

### 3. Operate Silently in Background Headless Browser

Use the `sessfork-browser` MCP tools (`browser_navigate`, `browser_snapshot`, `browser_click`, `browser_type`, `browser_take_screenshot`) to execute the web workflow in the background.

- **Dual-engine smart routing (`sessfork --engine dual`)**: If both `sessfork-moli` (managed Rust Moli headless browser) and `sessfork-browser` (`chrome-headless-shell`) are configured, prefer `sessfork-moli` first for fast DOM-first reading, research, and multi-context workloads (4.4x faster, 68.6% less RAM, 12.7 MB idle). If a site fails due to anti-bot checks or unsupported browser features on Moli, automatically fall back to `sessfork-browser` (Chrome stays unloaded at 0 MB until called).
- Note: Chromium batches `LocalStorage` flushes to disk every ~5 seconds; if the user *just* signed in a second ago and a token is missing, wait 5 seconds and re-run `sessfork <domain>` + `browser_set_storage_state`.
- macOS headless crash recovery: `sessfork --setup` automatically pins Playwright's `chrome-headless-shell` via `--executable-path` on macOS so multi-origin `storageState` files do not hit system Chrome's `Mach rendezvous failed` crash. If `sessfork-browser` fails with `Target page, context or browser has been closed`, run `npx -y playwright-core install chromium-headless-shell && sessfork --setup`, verify with `sessfork --status`, and ask the user to reload `sessfork-browser` once.

## Security Guardrails

1. **Least Privilege**: Always pass explicit `<domain>` names (never pass `--all-domains` unless the user explicitly asks for it).
2. **Zero Secret Leakage**: NEVER run `cat`, `head`, or `view_file` on `~/.sessfork/state.json`, `~/.sessfork/playwright-output/.sessfork-state.json`, or `~/.sessfork/keys.json`, and NEVER call `browser_storage_state`, `browser_cookie_list`, `browser_cookie_get`, `browser_localstorage_list`, `browser_localstorage_get`, `browser_sessionstorage_list`, or `browser_sessionstorage_get` or echo raw cookie/token values into chat or logs (`browser_set_storage_state` is safe because it only loads from disk and never echoes secrets). Use `sessfork --status` to inspect active counts safely.
3. **No Silent Key Caching**: Never pass `--cache-key` unless the user explicitly asks for it; it persists the derived cookie key on disk and weakens Keychain protection. If the user wants it removed, run `sessfork --purge-keys`.
