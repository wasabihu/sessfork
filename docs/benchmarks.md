# Stress Tests & Benchmarks

`sessfork` ships a zero-dependency, 100% local 5-scenario stress-test harness at [`scripts/stress_test.py`](../scripts/stress_test.py). It uses synthetic Chromium `Cookies` (SQLite WAL + AES-128-CBC v24) and `Local Storage/leveldb` (SSTable + WAL MVCC) fixtures plus a local `127.0.0.1` HTTP auth echo server — **zero external network calls, zero real user browser data, and zero OS Keychain prompts**.

## Reproducing Locally

```bash
# Full 5-scenario suite (includes live @playwright/mcp + chrome-headless-shell E2E)
python3 scripts/stress_test.py

# Include Scenario 6 (6x lightweight isolatedContext on local Moli binary)
python3 scripts/stress_test.py --include-moli

# Head-to-head 12x heavy-DOM (~1,775 nodes/tab) Chrome vs. Moli benchmark
# (separates pure browser engine RSS & process count from shared Node MCP RSS)
python3 scripts/stress_test.py --heavy-compare

# Fast CLI/storage/concurrency subset (Scenarios 1-3 only, ~2s)
python3 scripts/stress_test.py --skip-mcp
```

## Reference Results

Measured on **macOS 26.6.2 (Apple Silicon `arm64`)**, **Python 3.14.7**, **`@playwright/mcp@0.0.85`**, **`chrome-headless-shell` (rev 1254)**.

### 1. Extraction Scale & Throughput (SQLite AES-128-CBC + LevelDB SSTable/WAL)

Each domain has 20 encrypted v24 cookies + 20 LocalStorage keys (with WAL overrides and deletion tombstones).

| Tier | Domains | Cookies | LS Keys | `state.json` Size | Cookie Decrypt p50 | LevelDB Parse p50 | Atomic Write p50 | Total p50 / p95 | Cookie Throughput |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| Small | 10 | 200 | 200 | 96.7 KB | 1.35 ms | 1.33 ms | 0.82 ms | **3.64 ms** / 4.15 ms | 147,747 /s |
| Medium | 50 | 1,000 | 1,000 | 484.9 KB | 4.73 ms | 5.83 ms | 1.94 ms | **12.45 ms** / 13.01 ms | 211,543 /s |
| Large | 100 | 2,000 | 2,000 | 974.6 KB | 10.14 ms | 12.79 ms | 3.52 ms | **26.56 ms** / 27.16 ms | 197,229 /s |

### 2. Multi-Process Concurrent Fork Storm (Before vs. After `fcntl.flock`)

Multiple agent processes concurrently call `upsert_session_state()` for distinct domains against the same `state.json`, while a background reader continuously reads and parses `.sessfork-state.json`.

> **Real bug caught & fixed by this stress test:** Before adding cross-process `fcntl.flock(LOCK_EX)` locking around the read-modify-write + dual-mirror write cycle in `state.py`, individual `os.replace` writes prevented torn JSON (`0` parse errors), but parallel agents suffered **35.9%–57.5% silent lost updates**. Adding `0600` advisory file locks (`O_NOFOLLOW`) eliminated lost updates completely with ~11–31 ms total wall overhead.

| Workload | Expected Domains | Before Fix (Lost Updates) | After `fcntl.flock` (Lost Updates) | Mirror Diverged | Torn JSON Reads | Total Wall Time |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| 8 processes × 5 rounds | 40 | 23 lost (**57.5%**) | **0 lost (0.0%)** | `false` | 0 | 125.5 ms |
| 16 processes × 4 rounds | 64 | 23 lost (**35.9%**) | **0 lost (0.0%)** | `false` | 0 | 223.7 ms |

### 3. Live SQLite WAL Contention

A background writer continuously hammers a WAL-mode Chromium `Cookies` database (`5,795` commits + `231` `PRAGMA wal_checkpoint(PASSIVE)` calls) while 8 concurrent reader processes perform 200 total `extract_cookies()` runs.

| Metric | Result |
| :--- | :--- |
| Extractions succeeded | **200 / 200 (0.0% errors)** |
| Latency p50 / p95 / p99 | **32.69 ms** / 45.58 ms / 52.05 ms |
| Corrupted / missing cookie rows | **0** |

### 4. Live `@playwright/mcp` + `chrome-headless-shell` 50-Origin Hot-Reload Storm

A live `@playwright/mcp@0.0.85` stdio server starts with **50 origins / 981 cookies / 1,000 LocalStorage keys**, then runs **20 consecutive token-rotation cycles** (`upsert_session_state` → `browser_set_storage_state` → `browser_navigate` → auth echo check).

| Metric | Result |
| :--- | :--- |
| Cold-start 50-origin hydration | **642.4 ms** (verified, 0 crashes) |
| Hot-reload cycles passed | **20 / 20 (0 stale tokens, 0 crashes)** |
| `browser_set_storage_state` p50 / p95 | **263.8 ms** / 364.9 ms |
| `browser_navigate` p50 / p95 | **13.4 ms** / 16.7 ms |
| Process-tree RSS (after cold → after 20 cycles) | 545.5 MB → **590.4 MB** (`+44.9 MB`) |

### 5. Live `@playwright/mcp` Multi-Agent `isolatedContext` Isolation

Inside a single running `sessfork-browser` MCP server, 6 separate `isolatedContext` tabs are opened and each hydrated with a distinct user's Cookie + LocalStorage JWT, followed by interleaved out-of-order navigation checks across all 6 contexts.

| Metric | Result |
| :--- | :--- |
| Contexts initialized & verified | **6 / 6** |
| Interleaved verification passed | **6 / 6** |
| Cross-context cookie / LocalStorage leaks | **0** |
| Total RSS with 6 active isolated contexts | **1,218.9 MB** (~105 MB/context above base) |

### 6. Optional Lightweight Rust Stack: `chrome-headless-shell` vs. `moli` + Node MCP vs. `moli` + Native Rust `sessfork-mcp`

`sessfork` defaults to local `chrome-headless-shell` for maximum web compatibility (99.9% task pass rate on Lexbench), supports managed [`lexmount/moli`](https://github.com/lexmount/moli) over Playwright MCP, and ships an optional **605 KB native Rust CDP MCP server** ([`rust/sessfork-mcp`](../rust/sessfork-mcp)) that connects directly to `moli serve --remote-debugging-pipe` with **zero Node.js, zero `npx`, and zero TCP ports**.

When both `moli` and `sessfork-mcp` are installed (`cargo build --release` inside `rust/sessfork-mcp` and install to `~/.local/bin/sessfork-mcp`), `sessfork --engine moli` and `--engine dual` automatically wire the native Rust pipe runtime (and fresh `sessfork --setup` defaults straight to `dual` with Rust Moli primary + lazy `0 MB` Chrome fallback).

#### 6a. 6× Lightweight Auth Contexts & 50-Origin Hot-Reload

Measured on the same Apple Silicon macOS host against the same local `127.0.0.1` auth echo server:

| Metric | `chrome-headless-shell` + `@playwright/mcp` | `moli 1.1.27` + `@playwright/mcp` | **`moli 1.1.27` + `sessfork-mcp` (Rust Pipe)** | Delta vs. Chrome |
| :--- | :---: | :---: | :---: | :--- |
| **MCP Cold `initialize`** | `1,787.5 ms` | `1,229.5 ms` | **`4.6 ms`** | **`388×` faster** |
| **Idle MCP Wrapper RSS** | `~272 MB` (Node) | `~273 MB` (Node) | **`1.7 MB`** *(Moli lazy `0 MB`)* | **`~160×` smaller** |
| **50-Origin Hot-Reload p50** *(20 cycles)* | `263.8 ms` | `672.5 ms` | **`5.9 ms`** (`20/20` OK, 0 stale) | **`44.7×` faster** (`114×` vs Node+Moli) |
| **6× `isolatedContext` Isolation** | `6 / 6` (0 leaks) | `6 / 6` (0 leaks) | **`6 / 6` (0 leaks)** | Identical strict isolation |
| **6× `isolatedContext` Total Time** | `3,413.1 ms` | `778.6 ms` | **`230.4 ms`** | **`14.8×` faster** |
| **6× `isolatedContext` Total RSS** | `1,218.9 MB` | `382.5 MB` | **`84.8 MB`** (`82.2` Moli + `2.6` Rust) | **`14.4×` smaller (`-93.0%`)** |

#### 6b. 12× Heavy-DOM SaaS Dashboard Head-to-Head (`--heavy-compare`, 1,775 DOM nodes/tab)

All three stacks run the exact same realistic 12-agent authenticated SaaS dashboard (**120 table rows, rich CSS grid/gradients/badges, 1,775 DOM nodes/tab = 21,300 live DOM nodes**, 12 unique Cookie + LocalStorage JWTs, setup + interleaved out-of-order verification, **with full accessibility tree snapshots enabled on every navigation**):

| Metric (12× Heavy-DOM `isolatedContext`) | `chrome-headless-shell` + Node MCP | `moli 1.1.27` + Node MCP | **`moli 1.1.27` + Rust `sessfork-mcp`** | Rust Pipe vs. Chrome |
| :--- | :---: | :---: | :---: | :--- |
| **Total Active RSS** *(Browser + MCP)* | `2,531.4 MB` | `656.8 MB` | **`260.4 MB`** | **`9.72×` smaller (`-89.7%`, saves `2.27 GB`)** |
| **Pure Browser Engine RSS** | `2,259.7 MB` | `383.9 MB` | **`239.2 MB`** *(no Playwright JS heap!)* | **`9.45×` smaller (`-89.4%`)** |
| **MCP Wrapper RSS** | `271.7 MB` (Node) | `272.9 MB` (Node) | **`21.1 MB`** (`1.7 MB` idle) | **`12.9×` smaller** |
| **Browser OS Process Count** | `15` processes | `1` process | **`1` process** (+ 0 TCP ports) | **`15×` fewer OS processes** |
| **12× Heavy-DOM Total Elapsed Time** | `2,915.4 ms` | `5,543.9 ms` | **`1,501.0 ms`** | **`1.94×` faster** (`3.69×` vs Node+Moli) |
| **12× Setup + Interleaved Verify** | `12 / 12` (`0` leaks) | `12 / 12` (`0` leaks) | **`12 / 12` (`0` leaks)** | Zero cross-context leakage |

> **Why did replacing Node `@playwright/mcp` with pure Rust `sessfork-mcp` make Moli `3.69×` faster AND `2.5×` leaner even on Moli itself?**
> 1. Eliminates the fixed `~272 MB` Node.js runtime + `1.2–1.8 s` `npx` launch tax (`1,787 ms → 4.6 ms`).
> 2. Talks straight to `moli serve --remote-debugging-pipe` (OS FD 3/4 NUL-framed JSON, zero TCP ports, automatic kernel-level EOF cleanup even on `SIGKILL`).
> 3. Calls native CDP `Accessibility.getFullAXTree` + `Storage.setCookies` directly instead of injecting Playwright's large JS aria-snapshot runtime into all 12 V8 context heaps — cutting Moli's own browser RSS from `383.9 MB → 239.2 MB` and flipping heavy-DOM latency from `0.53×` slower than Chrome to **`1.94×` faster than Chrome**!
> 4. **Honest trade-off**: Moli's compatibility is 80.7% vs. Chrome's 99.9% on Lexbench (WebGL, complex anti-bot portals, Canvas). **`--engine dual`** remains the recommended daily driver: pure Rust `sessfork-moli` (`1.7 MB` idle) handles fast everyday tasks, while Chrome stays unloaded (`0 MB`) until fallback is needed.

## Caveats & Scope

- Scenarios 4–5 measure `@playwright/mcp` + `chrome-headless-shell` against a local `127.0.0.1` server; real-world external sites add network RTT and site-specific JS execution time.
- Websites that enforce **single-use refresh token rotation** can still invalidate forked sessions if both foreground Chrome and a headless context refresh concurrently (see the [README FAQ](../README.md#faq)).
