# Security Policy

`sessfork` handles live browser credentials, so security reports are taken seriously.

## Reporting a Vulnerability

Please **do not open a public issue** for security problems.

Use GitHub's private vulnerability reporting instead:
https://github.com/wasabihu/sessfork/security/advisories/new

Please include the affected version or commit, your OS, and steps to reproduce. You can expect an initial response within 7 days.

## Threat Model

**What `sessfork` is designed to protect against**

- Accidentally exporting credentials for sites you did not ask for (domain allowlist by default, rejecting blank inputs, bare TLDs, generic second-level ccTLDs, and shared cloud/hosting public suffixes like `github.io`, `appspot.com`, or `co.uk`).
- Other local OS users reading forked state, keys, screenshots, or driving Studio UI (`~/.sessfork/` and `playwright-output/` are `0700`; `state.json`, `keys.json`, newly created MCP configs, and `sessfork-mcp` screenshots are `0600` written atomically with `O_NOFOLLOW`; `sessfork --ui` enforces exact loopback `Host`/`Origin`/`Sec-Fetch-Site` validation, anti-clickjacking headers, and per-session authentication on all `GET` and `POST` routes).
- Cross-origin LocalStorage leakage and cross-origin iframe WebMCP spoofing inside native Rust `sessfork-mcp` (LocalStorage hydration runs in an isolated CDP JS world with per-origin filtering on hot-reload; page URLs/origins come from browser-authoritative CDP `Page.getFrameTree`; WebMCP tool selection defaults to same-origin frames).
- Leaking derived AES keys in process listings (`crypto.py` uses in-memory `CommonCrypto` on macOS and OpenSSL `libcrypto` via `ctypes` on Linux — no subprocess key arguments) and silently accepting corrupted cookies on stale cached keys (Chrome v24+ `SHA256(host_key)` prefixes are strictly verified).
- Unpinned floating supply-chain updates on install or MCP startup (`sessfork-browser` pins a tested `@playwright/mcp` version; `install.sh` pins release tags and enforces HTTPS TLS 1.2 + SHA-256 verification for prebuilt `sessfork-mcp` binaries).
- Symlinked browser database files or sidecars redirecting snapshot copies (`chrome.py`, `localstorage.py`, `state.py`, and `crypto.py` refuse symlinks before resolving paths and use `O_NOFOLLOW`).
- Credentials leaking into terminal output or AI agent context (`--status` / `--json` are redacted; native `sessfork-mcp` omits raw cookie/storage read tools and sanitizes parse errors).
- Your foreground browser being driven by an agent (forked state is used by a separate `--isolated` headless browser).

**What `sessfork` does NOT protect against**

- Malware or any process already running **as your own OS user**. Such a process can read `~/.sessfork/state.json` just like it can read Chrome's own profile.
- Multi-user exposure when using the **legacy Node/TCP fallback** (`sessfork --mcp-bridge moli` without native Rust `sessfork-mcp`): that fallback binds an unauthenticated loopback TCP CDP port on `127.0.0.1`. On shared multi-user hosts, install native Rust `sessfork-mcp`, which communicates exclusively over anonymous pipes (`--remote-debugging-pipe`) with zero TCP ports.
- Same-origin JavaScript state access when `browser_evaluate` is used on an authenticated page (note that `HttpOnly` cookies remain browser-enforced and unreadable from JS, while `localStorage` and non-`HttpOnly` cookies are accessible to page JS by browser design).
- Using `--cache-key`: this stores the derived cookie decryption key in `~/.sessfork/keys.json`, which lets any same-user process decrypt Chrome cookies **without** a Keychain prompt. It is off by default; remove it with `sessfork --purge-keys`.
- Misuse on devices or accounts you are not authorized to access. Only use `sessfork` on your own machines and accounts.

## Good Hygiene

- Fork only the domains a task needs; clear them afterwards with `sessfork --clear <domain>`.
- Never commit `state.json` / `keys.json` (the repo `.gitignore` already excludes them).
- Re-run `sessfork --refresh-key` if Chrome rotates its Safe Storage key.

## Supported Versions

Only the latest release receives security fixes.
