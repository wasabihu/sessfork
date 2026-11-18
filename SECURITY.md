# Security Policy

`sessfork` handles live browser credentials, so security reports are taken seriously.

## Reporting a Vulnerability

Please **do not open a public issue** for security problems.

Use GitHub's private vulnerability reporting instead:
https://github.com/wasabihu/sessfork/security/advisories/new

Please include the affected version or commit, your OS, and steps to reproduce. You can expect an initial response within 7 days.

## Threat Model

**What `sessfork` is designed to protect against**

- Accidentally exporting credentials for sites you did not ask for (domain allowlist by default).
- Other local OS users reading forked state (`~/.sessfork/` is `0700`, files are `0600`, written atomically).
- Credentials leaking into terminal output or AI agent context (`--status` / `--json` are redacted).
- Your foreground browser being driven by an agent (forked state is used by a separate headless browser).

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
