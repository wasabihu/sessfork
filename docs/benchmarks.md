# Stress Tests & Benchmarks

`sessfork` ships a zero-dependency, 100% local 5-scenario stress-test harness at [`scripts/stress_test.py`](../scripts/stress_test.py). It uses synthetic Chromium `Cookies` (SQLite WAL + AES-128-CBC v24) and `Local Storage/leveldb` (SSTable + WAL MVCC) fixtures plus a local `127.0.0.1` HTTP auth echo server — **zero external network calls, zero real user browser data, and zero OS Keychain prompts**.

## Reproducing Locally

```bash
# Full 5-scenario suite (includes live @playwright/mcp + chrome-headless-shell E2E)
python3 scripts/stress_test.py

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

## Caveats & Scope

- Scenarios 4–5 measure `@playwright/mcp` + `chrome-headless-shell` against a local `127.0.0.1` server; real-world external sites add network RTT and site-specific JS execution time.
- Websites that enforce **single-use refresh token rotation** can still invalidate forked sessions if both foreground Chrome and a headless context refresh concurrently (see the [README FAQ](../README.md#faq)).
