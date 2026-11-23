# sessfork-mcp (Native Rust CDP Pipe MCP Server)

A **605 KB**, **13-crate**, **zero-Node.js / zero-Tokio / zero-TCP-port** native Rust MCP server built for [`lexmount/moli`](https://github.com/lexmount/moli) and [`sessfork`](../../README.md).

It talks directly to `moli serve --remote-debugging-pipe` over OS file descriptors 3 & 4 (NUL-framed JSON), replacing the `~272 MB` Node.js `@playwright/mcp` wrapper and eliminating `npx` cold starts and orphan browser processes.

## Benchmarks (Apple Silicon, Full AX Tree Enabled)

Reproducible locally via `python3 scripts/stress_test.py --heavy-compare` (see [docs/benchmarks.md](../../docs/benchmarks.md)):

| Metric | Chrome + Node MCP | Moli + Node MCP | **Moli + `sessfork-mcp` (Rust Pipe)** |
| :--- | :---: | :---: | :---: |
| **MCP Cold `initialize`** | `1,787.5 ms` | `1,229.5 ms` | **`4.6 ms`** (`1.7 MB` idle RSS) |
| **50-Origin Hot-Reload p50** | `263.8 ms` | `672.5 ms` | **`5.9 ms`** (0 stale cookies/LS) |
| **6× Light Auth Time / RAM** | `3,413 ms` / `1,219 MB` | `779 ms` / `383 MB` | **`230 ms` / `85 MB`** (`14.8×` faster / `-93%`) |
| **12× Heavy-DOM RSS** *(1,775 nodes/tab)* | `2,531.4 MB` (`15` procs) | `656.8 MB` (`1` proc) | **`260.4 MB`** (`1` proc, **`9.72×` smaller**) |
| **12× Heavy-DOM Time** *(full AX tree)* | `2,915.4 ms` | `5,543.9 ms` | **`1,501.0 ms`** (**`1.94×` faster than Chrome**) |

## Install Standalone (No Python or Node.js Required)

### Option 1 · Prebuilt Binary (macOS arm64 / macOS x64 / Linux x64 / Linux arm64, with SHA-256 verification)

```bash
curl -fsSL https://raw.githubusercontent.com/wasabihu/sessfork/main/install.sh | bash -s -- --rust-mcp-only
```

Or download `sessfork-mcp-<os>-<arch>.tar.gz` and `SHA256SUMS.txt` directly from [GitHub Releases](https://github.com/wasabihu/sessfork/releases).

### Option 2 · Build from Source (Rust `>=1.75`, ~5 seconds)

```bash
# Directly from GitHub
cargo install --git https://github.com/wasabihu/sessfork sessfork-mcp

# Or from a local checkout (uses `install -m 755` for atomic Apple Silicon replacement)
cd rust/sessfork-mcp
cargo build --release
install -m 755 target/release/sessfork-mcp ~/.local/bin/sessfork-mcp
```

## Standalone MCP Configuration (Moli Only, Without `sessfork`)

`sessfork-mcp` works completely standalone with `moli` even without `--storage-state`:

```json
{
  "mcpServers": {
    "moli-browser": {
      "command": "/Users/you/.local/bin/sessfork-mcp",
      "args": [
        "--output-dir=/Users/you/.sessfork/playwright-output"
      ]
    }
  }
}
```

When used with `sessfork`, just run `sessfork --engine dual` (or `--engine moli`) — `sessfork` auto-detects `~/.local/bin/sessfork-mcp` and wires `--storage-state` and `--output-dir` automatically.

## Exposed Tools & Security Guarantees

- **13 core tools**: `browser_navigate`, `browser_snapshot`, `browser_tabs` (including named `isolatedContext` sandboxes), `browser_set_storage_state`, `browser_click`, `browser_type` (React/Vue compatible), `browser_press_key`, `browser_evaluate` (sync + async), `browser_navigate_back`, `browser_wait_for`, `browser_console_messages`, `browser_take_screenshot` (with `--layout`), and `browser_close`.
- **Zero raw secret export tools**: Deliberately omits `browser_cookie_list`, `browser_cookie_get`, `browser_storage_state`, and `browser_localstorage_get` so raw session tokens are never echoed into LLM context.
- **Strict `--output-dir` confinement**: Canonicalizes paths and rejects `..` traversal and symlinks escaping `--output-dir`.
- **Zero orphan `moli` processes**: Uses OS pipe FD 3/4 (`--remote-debugging-pipe`); even if `sessfork-mcp` receives `SIGKILL`, the kernel closes FD 3 and `moli` terminates cleanly.
