# Security Audit & Adversarial Verification Report

> **Scope:** `sessfork` (Python CLI, Studio UI, SQLite/LevelDB extractor, state store) and `sessfork-mcp` (native Rust CDP MCP server & Chrome/Edge Native Messaging host, `v0.2.1+`).
> **Verification Philosophy:** Every security claim below is backed by a **reproducible, zero-dependency adversarial verification script** ([`scripts/security_verify.py`](../scripts/security_verify.py)) plus unit test suites (`cargo test` + `unittest`) that anyone can rerun locally in seconds with zero external network calls and zero real user credentials.

## Quick Reproduction

```bash
# 1. Run live black-box & white-box adversarial verification (14 checks / 48+ vectors, ~3s)
python3 scripts/security_verify.py

# 2. Run Rust security & protocol unit tests (31 tests)
cd rust/sessfork-mcp && cargo test --locked --all-targets

# 3. Run Python security & state unit tests (42 tests)
PYTHONPATH=src python3 -m unittest discover -s tests
```

---

## Executive Summary

Measured on **macOS 26.6.2 (Apple Silicon `arm64`)**, **Python 3.14.7**, **`sessfork-mcp 0.2.1`** (`816.3 KiB` stripped binary), **`moli 1.1.27`**:

| Suite | Result | Scope |
| :--- | :---: | :--- |
| Live Adversarial Verification (`scripts/security_verify.py`) | **14 / 14 PASS (0 FAIL)** | 8 trust boundaries, 48+ concrete attack vectors against real `sessfork-mcp` binary & Python modules |
| Rust Unit & Security Tests (`cargo test --locked`) | **31 / 31 PASS** | SSRF `--cdp-url` allowlist, CDP pipe/WS framing, Native Messaging domain/cookie sanitization, screenshot/path confinement, isolated-world JS builder |
| Python Unit & Security Tests (`unittest`) | **42 / 42 PASS** | Domain allowlist, symlink rejection, SQLite/LevelDB parsing, `0600`/`0700` file permissions, Studio UI CSRF/Host/Origin/CSP, CLI redaction |
| Hard-Kill & Lifecycle Proofs (`scripts/bench_extended.py`) | **2 / 2 PASS** | `SIGKILL` leaves `0` orphan `moli` processes (`13.9 ms` pipe EOF exit); external `--cdp-url` Chrome stays alive with `0` leaked targets on both `browser_close` and stdin EOF |

---

## Architecture & Trust Boundaries

```text
  [Untrusted Web Page DOM / Main-World JS]
                     │
                     ▼  (Isolated CDP JS World + Browser-Authoritative Page.getFrameTree)
  [Headless Browser Engine: Moli Pipe (FD 3/4) or Loopback Chromium (--cdp-url)]
                     │
                     ▼  (Zero raw credential read tools; 0600 confined --output-dir)
  [sessfork-mcp (Native Rust Binary — Stdio MCP + Native Messaging Host)]
                     │
                     ▼  (0700 dir, 0600 O_NOFOLLOW atomic files, fcntl.flock / Win32 Job Object)
  [~/.sessfork/state.json & playwright-output/.sessfork-state.json]
                     ▲
                     │  (Domain allowlist, public-suffix guard, zero-echo redacted replies)
  [Chrome/Edge Companion Extension (1-Click User Gesture) OR Local CLI Extractor]
```

| Boundary | Threat Mitigated | Enforcement Mechanism |
| :--- | :--- | :--- |
| **External CDP Attach (`--cdp-url`)** | SSRF to cloud IMDS (`169.254.169.254`), RFC1918 LAN, public hosts, userinfo `@` spoofing, non-HTTP/WS schemes | Fail-closed `parse_loopback_cdp_endpoint()` accepts **only** `http://127.0.0.1:<port>`, `http://localhost:<port>`, `ws://127.0.0.1:<port>/<path>`, `ws://localhost:<port>/<path>`; exits code `2` before opening any socket |
| **MCP Navigation (`browser_navigate`)** | Local file read (`file://`), script injection (`javascript:`), `data:` phishing, internal `chrome://` pages | Explicit scheme allowlist (`http://` and `https://` only); all other schemes rejected fail-closed |
| **Multi-Origin Hydration & Hot-Reload** | Cross-origin `localStorage` leakage; hostile page hijacking `window.JSON.stringify` or `Object.prototype` | Hydration executes inside a dedicated CDP isolated JS world (`Page.createIsolatedWorld`); live hot-reload queries browser-authoritative `Page.getFrameTree` origin and injects **only** exact-origin entries |
| **MCP Tool Surface & CLI Output** | Prompt-injection exfiltrating raw `HttpOnly` cookies or JWTs into LLM context / terminal logs | Deliberately omits `browser_cookie_list`, `browser_cookie_get`, `browser_storage_state`, `browser_localstorage_get`, `browser_sessionstorage_get`; CLI `--status --json` and `native-host` replies emit counts/domains only, zero token strings |
| **Filesystem Sandbox (`--output-dir` & `~/.sessfork`)** | Path traversal (`../../`), symlink overwrite, hidden-file clobber, existence-oracle probing, multi-user local snooping | Canonicalized root checks; `O_NOFOLLOW` + pre-resolve symlink rejection; `0700` dirs & `0600` files; identical sanitized error string (`Access denied: path is outside allowed roots (--output-dir)`) for existing vs. nonexistent outside paths |
| **Native Messaging Host & CLI Domain Filter** | Broad cookie harvest (`co.uk`, `github.io`, `com`, blank), userinfo spoofing (`https://github.com@evil.com`), foreign-domain cookie injection, silent credential wipe on corrupt JSON | Strict RFC 1123 + shared public-suffix allowlist; rejects `@` userinfo in both Rust and Python; drops foreign-domain cookies/origins before writing; fail-closed on corrupt `state.json` |
| **Studio Control Panel (`sessfork --ui`)** | Cross-site CSRF, DNS rebinding, clickjacking, secret leakage in `ps aux` | Binds exclusively to `127.0.0.1`; enforces exact `Host`, `Origin`, `Sec-Fetch-Site`, and 256-bit `X-Sessfork-Token` / HttpOnly session cookie; `0600` auto-POST launcher avoids CLI argv secrets; strict `X-Frame-Options: DENY` + `CSP: frame-ancestors 'none'` |
| **Process Lifecycle (Unix & Windows)** | Orphan headless browsers lingering with active authenticated contexts after parent crash | Unix uses anonymous pipe FD 3/4 (`--remote-debugging-pipe`, kernel EOF exit on `SIGKILL`); Windows uses Win32 Job Object (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`); `--cdp-url` `BrowserManager::drop` disposes only `sessfork-mcp`-created isolated contexts without killing the host browser |

---

## Live Adversarial Verification Matrix (`scripts/security_verify.py`)

Every row below was executed directly against the compiled `sessfork-mcp 0.2.1` binary and Python `sessfork` modules:

| Domain | Check | Vectors Tested | Status | Verified Result |
| :--- | :--- | :---: | :---: | :--- |
| **D1 · SSRF (`--cdp-url`)** | `reject_all_14_ssrf_and_malformed_cdp_urls` | 14 (`169.254.169.254`, `10.0.0.1`, `192.168.1.1`, `evil.com`, `127.0.0.1@evil.com`, `user:pass@127.0.0.1`, `0.0.0.0`, port `0`, `https://`, `wss://`, `file://`, extra path, `#frag`) | **PASS** | `14/14` exited fail-closed with exit code `2` |
| **D2 · Navigation Sandbox** | `block_file_javascript_data_chrome_urls` | 4 (`file:///etc/passwd`, `javascript:alert(1)`, `data:text/html,...`, `chrome://settings`) | **PASS** | `4/4` rejected with MCP `isError: true` |
| **D3 · Cross-Origin & Isolated World** | `origin_scoped_hydration_under_main_world_tampering` | 2 origins + hostile `window.JSON.stringify` / `JSON.parse` / `Object.prototype` hijack + live hot-reload | **PASS** | `init_A_isolated=True`, `hot_reload_A_immune_to_JSON_hijack=True`, `origin_B_isolated=True` |
| **D4 · Filesystem & Symlinks** | `screenshot_confinement_symlink_block_and_0600_mode` | 5 attacks (`../../escape.png`, absolute path, `.sessfork-state.json`, `notes.txt`, symlink `sym.png`) + valid `.png` | **PASS** | `5/5` blocked, symlink target untouched, valid PNG mode `0o600` |
| **D4 · Existence Oracle** | `storage_state_outside_root_no_existence_oracle` | Existing outside file vs. nonexistent outside path | **PASS** | Both blocked with byte-for-byte identical sanitized error |
| **D4 · Python State Guards** | `python_state_refuses_symlink_and_enforces_0700_0600` | Symlinked `state.json` + fresh state write | **PASS** | Symlink refused, victim untouched, dir `0o700`, file `0o600` |
| **D5 · Native Host Domain Scope** | `reject_public_suffix_userinfo_and_path_spoof_domains` | 8 (`github.io`, `co.uk`, `com`, `""`, `https://github.com@evil.com`, `*.github.com`, `javascript://...`, `intranet`) | **PASS** | `8/8` rejected (`ok: false`) |
| **D5 · Native Host Filtering & Zero-Echo** | `drop_foreign_domain_cookies_and_origins_plus_zero_secret_echo` | Mixed `.example.com` + `.evil.com` + `evil.com/.example.com` cookies + foreign origin | **PASS** | Foreign/slash-injected cookies & origins dropped, `0o600` saved, `0` secret bytes in reply |
| **D5 · Fail-Closed State Integrity** | `corrupt_state_json_fails_closed_without_wipe` | Truncated/corrupt `state.json` before `sync` | **PASS** | Rejected (`ok: false`), original file preserved intact |
| **D5 · Native Host Symlink Guard** | `native_host_refuses_symlink_state_file` | Symlinked `~/.sessfork/state.json` | **PASS** | Rejected (`ok: false`), symlink target intact |
| **D6 · MCP Tool Surface** | `zero_raw_credential_reader_tools_exposed` | Live `tools/list` audit against 7 forbidden credential-read tool names | **PASS** | `16` tools exposed, `0` credential-read tools |
| **D6 · CLI Redaction** | `cli_status_json_strictly_redacts_all_secret_values` | Sentinel cookie + JWT in `state.json` via `--status --json` | **PASS** | Valid summary JSON, `0` sentinel bytes in stdout/stderr |
| **D7 · Studio UI (`--ui`)** | `enforce_session_auth_csrf_dns_rebinding_origin_and_csp` | 6 attacks (unauth GET `/`, unauth `/api/status`, `Host: evil.com`, `Sec-Fetch-Site: cross-site`, missing CSRF token, forged `Origin`) + authed header check | **PASS** | All `6` attacks returned HTTP `403`; authed `200` enforces `DENY`, `nosniff`, `no-store`, `no-referrer`, `frame-ancestors 'none'` |
| **D8 · Python Domain Allowlist** | `reject_blank_tlds_cctlds_and_shared_cloud_public_suffixes` | 9 (`""`, whitespace, `com`, `co.uk`, `github.io`, `appspot.com`, `vercel.app`, `amazonaws.com`, `https://github.com@evil.com`) | **PASS** | `9/9` rejected with `BrowserProfileError` |

---

## Real Defects Caught & Remediated During Verification

We explicitly log real bugs discovered while building and running these harnesses:

1. **Cross-implementation userinfo spoofing gap in Python `normalize_domain_filter()` (Fixed):**
   - *Finding:* Running `scripts/security_verify.py` (D8) revealed that Python's `urllib.parse.urlparse("https://github.com@evil.com").hostname` silently discarded `github.com@` and returned `"evil.com"`, whereas Rust `native_host.rs` already failed closed on `@` userinfo.
   - *Remediation:* Added explicit `parsed.username` / `parsed.password` / `"@" in parsed.netloc` rejection in [`src/sessfork/chrome.py`](../src/sessfork/chrome.py) plus regression unit tests in [`tests/test_sessfork.py`](../tests/test_sessfork.py).
2. **External CDP (`--cdp-url`) context leak on abrupt stdin EOF (Fixed):**
   - *Finding:* If an MCP host closed stdin without explicitly calling `browser_close`, `BrowserManager` dropped without sending `Target.disposeBrowserContext` to the external Chromium instance.
   - *Remediation:* Implemented `Drop` for `BrowserManager` in [`rust/sessfork-mcp/src/browser.rs`](../rust/sessfork-mcp/src/browser.rs) so created isolated contexts are disposed on both explicit `browser_close` and clean stdin EOF, verified at `0` leaked targets in `scripts/bench_extended.py`.
3. **Concurrent multi-process `state.json` lost updates (Previously caught & fixed):**
   - *Finding:* Stress testing 8–16 parallel agents exposed `35.9%–57.5%` silent lost updates during unsynchronized read-modify-write cycles.
   - *Remediation:* Added `0600` `O_NOFOLLOW` cross-process advisory locks (`fcntl.flock` / platform locks), reducing lost updates to `0.0%` (see [`docs/benchmarks.md`](benchmarks.md)).

---

## Honest Boundaries: Assumed, Out-of-Scope & Not Tested ≠ Clean

In accordance with rigorous security reporting, **`Not tested ≠ clean`**. The following boundaries are **outside** `sessfork`'s guarantees or have **not** been independently audited:

- **Same-UID local compromise (Out of Scope):** Any malware or process already running as your own OS user account can read `~/.sessfork/state.json` just as it can read Chrome's profile directory or inject into running user processes.
- **Same-origin `browser_evaluate` access (By Browser Design):** Calling `browser_evaluate` on an authenticated tab executes JavaScript inside that tab's origin. Browser-enforced `HttpOnly` cookies remain unreadable from JS, while `localStorage` and non-`HttpOnly` cookies are accessible to same-origin JS by web-platform design.
- **Opt-in `--cache-key` (Off by Default):** Enabling `--cache-key` persists the derived cookie AES key in `~/.sessfork/keys.json` (`0600`), allowing any same-user process to decrypt Chrome cookies without triggering an OS Keychain prompt. Clear with `sessfork --purge-keys`.
- **Legacy Node/TCP fallback (`--mcp-bridge moli` without Rust `sessfork-mcp`):** Binds an unauthenticated loopback TCP CDP port on `127.0.0.1`. On shared multi-user hosts, use native Rust `sessfork-mcp` with Unix anonymous pipe FD 3/4 (`--remote-debugging-pipe`).
- **Single-use OAuth/OIDC refresh-token rotation:** Websites that invalidate a session immediately when two clients refresh the same refresh token concurrently can log out either foreground Chrome or the forked background session.
- **Upstream browser engine vulnerabilities:** `sessfork` does not sandbox or patch Chromium, Moli, V8, or OS Keychain/DPAPI/ABE internals; engine security depends on keeping your installed Chromium/Moli binaries up to date.
- **What has NOT been run:** No third-party commercial penetration test, no formal memory-safety fuzzing campaign (`cargo-fuzz`) against the pure-std RFC 6455 WebSocket parser, and no Linux multi-user SELinux/AppArmor confinement audit have been performed yet.

For vulnerability disclosure instructions, see [`SECURITY.md`](../SECURITY.md).
