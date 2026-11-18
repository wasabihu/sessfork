# Security Policy

`sessfork` handles live browser credentials, so security reports are taken seriously.

## Reporting a Vulnerability

Please **do not open a public issue** for security problems.

Use GitHub's private vulnerability reporting instead:
https://github.com/wasabihu/sessfork/security/advisories/new

Please include the affected version or commit, your OS, and steps to reproduce. You can expect an initial response within 7 days.

## Threat Model

**What `sessfork` is designed to protect against**

- Accidentally exporting credentials for sites you did not ask for (domain allowlist by default, rejecting bare TLDs and shared public suffixes like `github.io` or `co.uk`).
- Other local OS users reading forked state or keys (`~/.sessfork/` and `playwright-output/` are `0700`; `state.json`, `keys.json`, and newly created MCP configs are `0600`, written atomically).
- Leaking derived AES keys in process listings (`crypto.py` uses in-memory `CommonCrypto` on macOS and OpenSSL `libcrypto` via `ctypes` on Linux — no subprocess key arguments).
- Unpinned floating supply-chain updates on MCP startup (`sessfork-browser` pins a tested `@playwright/mcp` version rather than `@latest`).
- Symlinked browser database files redirecting snapshot copies (`chrome.py` and `localstorage.py` refuse/skip symlinks).
- Credentials leaking into terminal output or AI agent context (`--status` / `--json` are redacted).
- Your foreground browser being driven by an agent (forked state is used by a separate `--isolated` headless browser).

**What `sessfork` does NOT protect against**

- Malware or any process already running **as your own OS user**. Such a process can read `~/.sessfork/state.json` just like it can read Chrome's own profile.
- Using `--cache-key`: this stores the derived cookie decryption key in `~/.sessfork/keys.json`, which lets any same-user process decrypt Chrome cookies **without** a Keychain prompt. It is off by default; remove it with `sessfork --purge-keys`.
- Misuse on devices or accounts you are not authorized to access. Only use `sessfork` on your own machines and accounts.

## Good Hygiene

- Fork only the domains a task needs; clear them afterwards with `sessfork --clear <domain>`.
- Never commit `state.json` / `keys.json` (the repo `.gitignore` already excludes them).
- Re-run `sessfork --refresh-key` if Chrome rotates its Safe Storage key.

## Supported Versions

Only the latest release receives security fixes.
