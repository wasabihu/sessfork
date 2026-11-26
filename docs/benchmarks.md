# Stress Tests & Benchmarks

`sessfork` ships two zero-dependency, 100% local benchmark harnesses — [`scripts/stress_test.py`](../scripts/stress_test.py) and [`scripts/bench_extended.py`](../scripts/bench_extended.py). Both run against synthetic Chromium `Cookies` (SQLite WAL + AES-128-CBC v24) and `Local Storage/leveldb` (SSTable + WAL MVCC) fixtures plus a local `127.0.0.1` HTTP auth echo server — **zero external network calls, zero real user browser data, and zero OS Keychain prompts**.

## Reproducing Locally

```bash
# Extended 4-way 12x Heavy-DOM (3 rounds), 100-cycle memory soak, SIGKILL & --cdp-url detach proofs
python3 scripts/bench_extended.py --rounds 3 --soak-cycles 100

# Full 5-scenario suite (includes live @playwright/mcp + chrome-headless-shell E2E)
python3 scripts/stress_test.py

# Head-to-head 12x heavy-DOM (~1,775 nodes/tab) Chrome vs. Moli benchmark
python3 scripts/stress_test.py --heavy-compare

# Fast CLI/storage/concurrency subset (Scenarios 1-3 only, ~2s)
python3 scripts/stress_test.py --skip-mcp
```

---

## Executive Summary: 4-Way 12× Heavy-DOM Head-to-Head (`--cdp-url` Isolates Pure MCP Wrapper Gain)

Measured on **macOS 26.6.2 (Apple Silicon `arm64`)**, **Python 3.14.7**, **`chrome-headless-shell` (`HeadlessChrome/159.0.8154.6`)**, **`moli 1.1.27`**, **`@playwright/mcp@0.0.85`**, **`sessfork-mcp 0.2.1`** (`816.3 KiB` stripped binary).

All **4 stacks** execute the exact same 12-agent authenticated SaaS dashboard workload (**120 table rows, 1,775 DOM nodes/tab = 21,300 live DOM nodes**, 12 unique Cookie + LocalStorage JWTs, setup + interleaved out-of-order verification, **full AX tree snapshots enabled on every navigation**), across **3 measured rounds** (`12/12` isolation checks passed with **`0` cross-context leaks** on every round across all 4 stacks):

| Metric (3-Round Median, 12× Heavy-DOM `isolatedContext`) | `chrome-headless-shell` + Node `@playwright/mcp` | **`chrome-headless-shell` + Rust `sessfork-mcp --cdp-url`** | `moli 1.1.27` + Node `@playwright/mcp` | **`moli 1.1.27` + Rust `sessfork-mcp` (Unix Pipe)** |
| :--- | :---: | :---: | :---: | :---: |
| **MCP Cold `initialize`** *(min–max)* | `1,240.4 ms` *(1,232.7–1,585.9)* | **`3.2 ms`** *(2.9–22.6, **`388×` faster**)* | `1,260.5 ms` *(1,194.8–2,207.0)* | **`2.8 ms`** *(2.7–3.1, **`443×` faster**)* |
| **12× Heavy-DOM Total Time** *(min–max)* | `2,357.9 ms` *(2,349.1–2,535.8)* | **`1,527.8 ms`** *(1,523.4–1,543.3, **`1.54×` faster**)* | `5,451.2 ms` *(5,421.9–5,605.5)* | **`1,509.2 ms`** *(1,491.8–1,544.2, **`1.56×` faster**)* |
| **Total Active RSS** *(Browser + MCP)* | `2,715.1 MB` *(2,708.5–2,716.3)* | **`1,967.3 MB`** *(saves **`747.8 MB` / `-27.5%`**)* | `652.4 MB` *(643.2–653.3)* | **`273.8 MB`** *(**`9.92×` smaller / `-89.9%`**, saves `2.44 GB`)* |
| **Pure Browser Engine RSS** | `2,417.0 MB` | **`1,914.0 MB`** *(`-503.0 MB` on identical Chrome!)* | `381.5 MB` | **`253.0 MB`** *(`-128.5 MB` vs Node+Moli)* |
| **MCP Wrapper RSS** *(under 12× load)* | `298.4 MB` (Node) | **`53.9 MB`** *(Rust peak; `2.1 MB` idle)* | `270.0 MB` (Node) | **`20.7 MB`** *(Rust peak; `1.7 MB` idle)* |
| **Browser OS Process Count** | `15` procs | `16` procs | `1` proc | **`1` proc** (+ `0` TCP ports) |
| **12× Isolation Verification (All Rounds)** | `12 / 12` (`0` leaks) | **`12 / 12` (`0` leaks)** | `12 / 12` (`0` leaks) | **`12 / 12` (`0` leaks)** |

> **Key Architectural Finding — Pure MCP Wrapper Savings on Identical Chromium:**
> Even **without** switching browser engines, replacing Node.js `@playwright/mcp` with pure Rust `sessfork-mcp --cdp-url` on the **exact same `chrome-headless-shell` binary**:
> 1. Cuts cold MCP `initialize` latency by **`388×`** (`1,240.4 ms → 3.2 ms`).
> 2. Cuts 12-context Heavy-DOM wall time by **`1.54×`** (`2,357.9 ms → 1,527.8 ms`).
> 3. Saves **`747.8 MB` (`-27.5%`) total RAM** — not just from removing the `298.4 MB` Node.js wrapper, but also cutting **Chrome's own renderer RSS by `503.0 MB`** (`2,417.0 MB → 1,914.0 MB`) because `sessfork-mcp` calls native CDP `Accessibility.getFullAXTree` directly instead of injecting Playwright's JS aria-snapshot runtime into all 12 V8 context heaps.

### Independent Verification by the `lexmount/moli` Team (Apple M4, 24 GB, 5-Run Medians)

In [lexmount/moli Discussion #3465](https://github.com/lexmount/moli/discussions/3465#discussioncomment-19846839), the Moli team independently reran `python3 scripts/stress_test.py --heavy-compare` on an **Apple M4 (24 GB RAM)** with **1 warm-up + 5 measured runs** (`12/12` isolation checks passed on every run):

| Stack (Apple M4 24 GB, 5-run median) | Total Elapsed Time | Total End RSS | Delta vs. Chrome |
| :--- | :---: | :---: | :--- |
| `chrome-headless-shell` + `@playwright/mcp` | `11.65 s` | `2,439.5 MiB` | baseline |
| `moli` + `@playwright/mcp` (Node) | `25.41 s` | `575.6 MiB` | `-76.4%` RAM, `0.46×` speed |
| **`moli` + `sessfork-mcp` (Rust Pipe)** | **`4.22 s`** | **`226.3 MiB`** | **`-90.7%` RAM (`10.78×` smaller), `2.76×` faster** (`6.02×` faster than Node+Moli) |

---

## 100-Cycle Token-Rotation Hot-Reload Memory Soak (`scripts/bench_extended.py`)

Runs **100 consecutive token-rotation cycles** (`write fresh state` → `browser_set_storage_state` → `browser_navigate` → assert fresh Cookie & LocalStorage tokens with zero stale reads) to verify long-running stability and isolate wrapper vs. browser memory growth:

| Stack (100 Consecutive Cycles) | Passed / Stale | Total Wall Time | Hot-Reload p50 / p95 / p99 | Nav p50 / p95 | Rust Wrapper RSS (c1 → c10 → c25 → c50 → c100) | Browser RSS (c1 → c10 → c50 → c100) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **`moli 1.1.27` + `sessfork-mcp` (Pipe)** | **`100 / 100`** (`0` stale) | **`670.6 ms`** | **`0.85 ms`** / `1.07 ms` / `1.52 ms` | `3.50 ms` / `4.66 ms` | `2.2 → 2.6 → 2.8 → 2.8 → 2.8 MB` (**`+0.0 MB` c25→c100**) | `47.2 → 59.2 → 91.8 → 92.9 MB` *(plateaus at ~92 MB)* |
| **`chrome-headless-shell 159` + `sessfork-mcp --cdp-url`** | **`100 / 100`** (`0` stale) | **`807.9 ms`** | **`1.01 ms`** / `1.33 ms` / `1.74 ms` | `3.66 ms` / `4.27 ms` | `2.1 → 2.6 → 2.8 → 2.8 → 2.8 MB` (**`+0.0 MB` c25→c100**) | `254.4 → 278.1 → 377.0 → 496.3 MB` *(Chromium BFCache/history)* |

> **Honest Engineering Note:** Across 100 consecutive hot-reload navigations, **`sessfork-mcp` itself stays completely flat at `2.8 MB` RSS (`+0.0 MB` from cycle 25 through cycle 100)** on both engines, confirming zero memory leaks in the Rust MCP server. On the engine side, Moli plateaus cleanly around `92 MB` (`+1.1 MB` across cycles 50→100), whereas `chrome-headless-shell` retains BFCache / navigation history across rapid unique-query navigations (`+2.4 MB/nav` before GC pressure).

---

## Process Lifecycle & Orphan-Freedom Proofs (`scripts/bench_extended.py` & Real Windows 10)

| Proof | Platform & Transport | Verification Procedure | Measured Outcome |
| :--- | :--- | :--- | :--- |
| **`SIGKILL` Hard-Kill Orphan Proof** | macOS `arm64`, Unix FD 3/4 pipe (`--remote-debugging-pipe`) | Launch `sessfork-mcp` + `moli`, load 3 active isolated contexts, `kill -9` parent `sessfork-mcp` | **`0` surviving `moli` orphans**; child auto-exited in **`13.9 ms`** on kernel pipe EOF |
| **External `--cdp-url` Non-Destruction** | macOS `arm64`, pure-std RFC 6455 WS to `HeadlessChrome/159` | Attach via `--cdp-url`, open isolated contexts, test both explicit `browser_close` AND abrupt stdin EOF (`Drop`) | Chrome stayed alive (`true`) and **`0` leaked targets** in `/json/list` for both paths |
| **Real Windows 10 Native QA (`v0.2.1`)** | Windows 10 x64 + Chrome 158 (`v20` ABE) + Win32 Job Object | Zero-flag auto-discovery of `%LOCALAPPDATA%\Moli\bin\moli.exe`, live MCP tools, `Stop-Process -Force` | Cold `initialize` **`10.8 ms`**, loopback navigate `94.1 ms`, `browser_extract_markdown` **`5.7 ms`**, `browser_close` `31.6 ms`, **`0` surviving `moli.exe` orphans**; Companion Extension synced `17` cookies (`11 HttpOnly`) + `128` localStorage keys cleanly |

---

## Core Storage, Concurrency & Playwright Baseline Benchmarks (`scripts/stress_test.py`)

### 1. Extraction Scale & Throughput (SQLite AES-128-CBC + LevelDB SSTable/WAL)

Each domain has 20 encrypted v24 cookies + 20 LocalStorage keys (with WAL overrides and deletion tombstones):

| Tier | Domains | Cookies | LS Keys | `state.json` Size | Cookie Decrypt p50 | LevelDB Parse p50 | Atomic Write p50 | Total p50 / p95 | Cookie Throughput |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| Small | 10 | 200 | 200 | 96.7 KB | 1.35 ms | 1.33 ms | 0.82 ms | **3.64 ms** / 4.15 ms | 147,747 /s |
| Medium | 50 | 1,000 | 1,000 | 484.9 KB | 4.73 ms | 5.83 ms | 1.94 ms | **12.45 ms** / 13.01 ms | 211,543 /s |
| Large | 100 | 2,000 | 2,000 | 974.6 KB | 10.14 ms | 12.79 ms | 3.52 ms | **26.56 ms** / 27.16 ms | 197,229 /s |

### 2. Multi-Process Concurrent Fork Storm (Before vs. After `fcntl.flock`)

> **Real bug caught & fixed by this stress test:** Before adding cross-process `fcntl.flock(LOCK_EX)` locking around the read-modify-write + dual-mirror write cycle in `state.py`, individual `os.replace` writes prevented torn JSON (`0` parse errors), but parallel agents suffered **35.9%–57.5% silent lost updates**. Adding `0600` advisory file locks (`O_NOFOLLOW`) eliminated lost updates completely with ~11–31 ms total wall overhead.

| Workload | Expected Domains | Before Fix (Lost Updates) | After `fcntl.flock` (Lost Updates) | Mirror Diverged | Torn JSON Reads | Total Wall Time |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| 8 processes × 5 rounds | 40 | 23 lost (**57.5%**) | **0 lost (0.0%)** | `false` | 0 | 125.5 ms |
| 16 processes × 4 rounds | 64 | 23 lost (**35.9%**) | **0 lost (0.0%)** | `false` | 0 | 223.7 ms |

### 3. Live SQLite WAL Contention

A background writer continuously hammers a WAL-mode Chromium `Cookies` database (`5,795` commits + `231` `PRAGMA wal_checkpoint(PASSIVE)` calls) while 8 concurrent reader processes perform 200 total `extract_cookies()` runs:

| Metric | Result |
| :--- | :--- |
| Extractions succeeded | **200 / 200 (0.0% errors)** |
| Latency p50 / p95 / p99 | **32.69 ms** / 45.58 ms / 52.05 ms |
| Corrupted / missing cookie rows | **0** |

### 4. Lightweight 6× Auth Contexts & 50-Origin Hot-Reload

| Metric | `chrome-headless-shell` + `@playwright/mcp` | `moli 1.1.27` + `@playwright/mcp` | **`moli 1.1.27` + `sessfork-mcp` (Rust Pipe)** | Delta vs. Chrome |
| :--- | :---: | :---: | :---: | :--- |
| **MCP Cold `initialize`** | `1,787.5 ms` | `1,229.5 ms` | **`4.6 ms`** | **`388×` faster** |
| **Idle MCP Wrapper RSS** | `~272 MB` (Node) | `~273 MB` (Node) | **`1.7 MB`** *(Moli lazy `0 MB`)* | **`~160×` smaller** |
| **50-Origin Hot-Reload p50** *(20 cycles)* | `263.8 ms` | `672.5 ms` | **`5.9 ms`** (`20/20` OK, 0 stale) | **`44.7×` faster** (`114×` vs Node+Moli) |
| **6× `isolatedContext` Total Time** | `3,413.1 ms` | `778.6 ms` | **`230.4 ms`** | **`14.8×` faster** |
| **6× `isolatedContext` Total RSS** | `1,218.9 MB` | `382.5 MB` | **`84.8 MB`** (`82.2` Moli + `2.6` Rust) | **`14.4×` smaller (`-93.0%`)** |

## Caveats & Scope

- Local benchmarks measure against a loopback `127.0.0.1` server; real-world external sites add network RTT and site-specific JS execution time.
- **Engine compatibility trade-off:** Moli's compatibility is 80.7% vs. Chrome's 99.9% on Lexbench (WebGL, strict anti-bot portals, Canvas). **`--engine dual`** (or attaching `sessfork-mcp --cdp-url` to Chromium/CEF when full Blink fidelity is required) gives you the best of both worlds.
- Websites that enforce **single-use refresh token rotation** can still invalidate forked sessions if both foreground Chrome and a headless context refresh concurrently (see the [README FAQ](../README.md#faq)).
- For security threat boundaries and reproducible adversarial verification, see [`docs/security-report.md`](security-report.md).
