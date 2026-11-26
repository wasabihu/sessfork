#!/usr/bin/env python3
"""Extended reproducible performance, memory-soak, and hard-kill benchmark harness for sessfork.

Runs 100% locally against a loopback 127.0.0.1 HTTP server (zero external network,
zero real user browser data, zero OS Keychain prompts).

Suites:
  1. 4-way 12x Heavy-DOM head-to-head (N measured rounds, median + min/max):
       - chrome-headless-shell + @playwright/mcp (Node)
       - chrome-headless-shell + sessfork-mcp --cdp-url (Rust, isolates pure MCP wrapper gain!)
       - moli + @playwright/mcp (Node)
       - moli + sessfork-mcp (Rust Pipe FD 3/4)
  2. 100-cycle token-rotation hot-reload soak test (memory leak proof):
       - moli + sessfork-mcp (Rust Pipe)
       - chrome-headless-shell + sessfork-mcp --cdp-url (Rust)
  3. Process lifecycle & orphan-freedom proof:
       - SIGKILL hard-kill on sessfork-mcp (Unix pipe) -> 0 surviving moli orphans
       - External --cdp-url detach (browser_close & stdin EOF) -> Chrome stays alive, 0 leaked contexts
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import platform
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from sessfork.mcp_setup import (  # noqa: E402
    DEFAULT_PLAYWRIGHT_MCP_PKG,
    build_playwright_mcp_entry,
    find_playwright_headless_shell,
    resolve_npx_command,
)
from sessfork.moli_bridge import (  # noqa: E402
    find_moli_binary,
    find_rust_mcp_binary,
    get_moli_version,
    get_rust_mcp_version,
)
from sessfork.state import hot_reload_state_path, upsert_session_state  # noqa: E402
from stress_test import (  # noqa: E402
    LocalAuthHandler,
    MCPStdioClient,
    _extract_mcp_text,
    _run_heavy_workload_on_client,
    get_process_tree_breakdown,
    get_process_tree_rss_mb,
    pct,
)


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_cdp_http(port: int, timeout: float = 10.0) -> Dict[str, Any]:
    deadline = time.perf_counter() + timeout
    last_err: Optional[Exception] = None
    while time.perf_counter() < deadline:
        try:
            raw = urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=0.5).read()
            return json.loads(raw.decode("utf-8"))
        except Exception as exc:
            last_err = exc
            time.sleep(0.025)
    raise RuntimeError(f"CDP endpoint 127.0.0.1:{port} did not become ready: {last_err}")


def _list_child_pids(parent_pid: int) -> List[int]:
    try:
        out = subprocess.check_output(["ps", "-eo", "pid,ppid"], text=True, timeout=5)
    except Exception:
        return []
    children: Dict[int, List[int]] = {}
    for ln in out.splitlines()[1:]:
        parts = ln.strip().split()
        if len(parts) < 2:
            continue
        try:
            p, pp = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        children.setdefault(pp, []).append(p)
    result: List[int] = []
    stack = list(children.get(parent_pid, []))
    seen = set()
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        result.append(cur)
        stack.extend(children.get(cur, []))
    return result


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _rust_moli_tree_rss(root_pid: int) -> Dict[str, Any]:
    try:
        ps_out = subprocess.check_output(["ps", "-eo", "pid,ppid,rss,comm"], text=True, timeout=5)
    except Exception:
        return {"browser_rss_mb": 0.0, "browser_procs": 0, "node_mcp_rss_mb": 0.0, "total_rss_mb": 0.0}
    pm: Dict[int, Tuple[int, str]] = {}
    ch: Dict[int, List[int]] = {}
    for ln in ps_out.splitlines()[1:]:
        parts = ln.strip().split(None, 3)
        if len(parts) < 4:
            continue
        try:
            p, pp, r = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        pm[p] = (r, parts[3].strip())
        ch.setdefault(pp, []).append(p)
    w_kb = b_kb = b_cnt = 0
    stk = [root_pid]
    seen = set()
    while stk:
        cur = stk.pop()
        if cur in seen:
            continue
        seen.add(cur)
        if cur in pm:
            r_kb, cm = pm[cur]
            if "sessfork-mcp" in cm:
                w_kb += r_kb
            else:
                b_kb += r_kb
                b_cnt += 1
        stk.extend(ch.get(cur, []))
    return {
        "browser_rss_mb": round(b_kb / 1024.0, 1),
        "browser_procs": b_cnt,
        "node_mcp_rss_mb": round(w_kb / 1024.0, 1),
        "total_rss_mb": round((b_kb + w_kb) / 1024.0, 1),
    }


def _launch_chrome_cdp(chrome_bin: Path, user_data: Path, cdp_port: int) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [
            str(chrome_bin),
            "--headless",
            "--no-first-run",
            "--disable-gpu",
            "--disable-background-networking",
            f"--remote-debugging-port={cdp_port}",
            f"--user-data-dir={user_data}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _terminate_proc(proc: Optional[subprocess.Popen[Any]]) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            print(f"[warn] child process {proc.pid} did not exit within 3s after SIGKILL", file=sys.stderr)


def _run_heavy_chrome_node(origin: str, n_contexts: int) -> Dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="sf_ext_cn_") as tmp:
        tmp_dir = Path(tmp)
        state_file = tmp_dir / "state.json"
        upsert_session_state([], new_origins=[], state_path=state_file, replace_all=True)
        mirror = hot_reload_state_path(state_file)
        entry = build_playwright_mcp_entry(state_path=state_file)
        client = MCPStdioClient(entry["command"], entry["args"], entry["env"])
        try:
            t0 = time.perf_counter()
            client.initialize()
            init_ms = round((time.perf_counter() - t0) * 1000, 1)
            res = _run_heavy_workload_on_client(
                client,
                mirror.parent,
                origin,
                n_contexts,
                "cn",
                lambda: get_process_tree_breakdown(client.proc.pid),
            )
            res["mcp_init_ms"] = init_ms
            res["wrapper_rss_mb"] = res.pop("node_mcp_rss_mb", 0.0)
            return res
        finally:
            client.close()


def _run_heavy_chrome_rust(
    chrome_bin: Path, rust_mcp_bin: Path, origin: str, n_contexts: int
) -> Dict[str, Any]:
    cdp_port = _free_loopback_port()
    with tempfile.TemporaryDirectory(prefix="sf_ext_cr_") as tmp:
        tmp_dir = Path(tmp)
        out_dir = tmp_dir / "out"
        out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        user_data = tmp_dir / "profile"
        chrome_proc = _launch_chrome_cdp(chrome_bin, user_data, cdp_port)
        client: Optional[MCPStdioClient] = None
        try:
            _wait_cdp_http(cdp_port)
            t0 = time.perf_counter()
            client = MCPStdioClient(
                str(rust_mcp_bin),
                [f"--cdp-url=http://127.0.0.1:{cdp_port}", f"--output-dir={out_dir}"],
                {},
            )
            client.initialize()
            init_ms = round((time.perf_counter() - t0) * 1000, 1)

            def _rss() -> Dict[str, Any]:
                c_info = get_process_tree_breakdown(chrome_proc.pid)
                r_info = get_process_tree_breakdown(client.proc.pid)  # type: ignore[union-attr]
                return {
                    "browser_rss_mb": c_info["total_rss_mb"],
                    "browser_procs": c_info["total_procs"],
                    "node_mcp_rss_mb": r_info["total_rss_mb"],
                    "total_rss_mb": round(c_info["total_rss_mb"] + r_info["total_rss_mb"], 1),
                }

            res = _run_heavy_workload_on_client(client, out_dir, origin, n_contexts, "cr", _rss)
            res["mcp_init_ms"] = init_ms
            res["wrapper_rss_mb"] = res.pop("node_mcp_rss_mb", 0.0)
            return res
        finally:
            if client is not None:
                client.close()
            _terminate_proc(chrome_proc)


def _run_heavy_moli_node(moli_bin: Path, origin: str, n_contexts: int) -> Dict[str, Any]:
    cdp_port = _free_loopback_port()
    cdp_url = f"http://127.0.0.1:{cdp_port}"
    with tempfile.TemporaryDirectory(prefix="sf_ext_mn_") as tmp:
        out_dir = Path(tmp) / "out"
        out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        moli_proc = subprocess.Popen(
            [str(moli_bin), "serve", "--host", "127.0.0.1", "--port", str(cdp_port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        client: Optional[MCPStdioClient] = None
        try:
            _wait_cdp_http(cdp_port)
            npx = resolve_npx_command()
            t0 = time.perf_counter()
            client = MCPStdioClient(
                npx,
                [
                    "-y",
                    DEFAULT_PLAYWRIGHT_MCP_PKG,
                    f"--cdp-endpoint={cdp_url}",
                    "--caps=storage",
                    f"--output-dir={out_dir}",
                ],
                {},
            )
            client.initialize()
            init_ms = round((time.perf_counter() - t0) * 1000, 1)

            def _rss() -> Dict[str, Any]:
                m_info = get_process_tree_breakdown(moli_proc.pid)
                n_info = get_process_tree_breakdown(client.proc.pid)  # type: ignore[union-attr]
                return {
                    "browser_rss_mb": m_info["total_rss_mb"],
                    "browser_procs": m_info["total_procs"],
                    "node_mcp_rss_mb": n_info["total_rss_mb"],
                    "total_rss_mb": round(m_info["total_rss_mb"] + n_info["total_rss_mb"], 1),
                }

            res = _run_heavy_workload_on_client(client, out_dir, origin, n_contexts, "mn", _rss)
            res["mcp_init_ms"] = init_ms
            res["wrapper_rss_mb"] = res.pop("node_mcp_rss_mb", 0.0)
            return res
        finally:
            if client is not None:
                client.close()
            _terminate_proc(moli_proc)


def _run_heavy_moli_rust(moli_bin: Path, rust_mcp_bin: Path, origin: str, n_contexts: int) -> Dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="sf_ext_mr_") as tmp:
        out_dir = Path(tmp) / "out"
        out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        t0 = time.perf_counter()
        client = MCPStdioClient(
            str(rust_mcp_bin),
            [f"--moli={moli_bin}", f"--output-dir={out_dir}"],
            {},
        )
        try:
            client.initialize()
            init_ms = round((time.perf_counter() - t0) * 1000, 1)
            res = _run_heavy_workload_on_client(
                client,
                out_dir,
                origin,
                n_contexts,
                "mr",
                lambda: _rust_moli_tree_rss(client.proc.pid),
            )
            res["mcp_init_ms"] = init_ms
            res["wrapper_rss_mb"] = res.pop("node_mcp_rss_mb", 0.0)
            return res
        finally:
            client.close()


def _summarize_runs(runs: List[Dict[str, Any]]) -> Dict[str, Any]:
    def _med(key: str) -> float:
        vals = [float(r[key]) for r in runs]
        return round(float(statistics.median(vals)), 1)

    def _minmax(key: str) -> List[float]:
        vals = [float(r[key]) for r in runs]
        return [round(min(vals), 1), round(max(vals), 1)]

    return {
        "rounds": len(runs),
        "all_isolation_passed": all(
            r["setup_verified"] == r["isolated_contexts"]
            and r["interleaved_passed"] == r["isolated_contexts"]
            and r["cross_context_leaks"] == 0
            for r in runs
        ),
        "dom_nodes_per_tab": runs[0]["dom_nodes_per_tab"] if runs else 0,
        "mcp_init_ms_median": _med("mcp_init_ms"),
        "mcp_init_ms_range": _minmax("mcp_init_ms"),
        "total_ms_median": _med("total_ms"),
        "total_ms_range": _minmax("total_ms"),
        "total_rss_mb_median": _med("total_rss_mb"),
        "total_rss_mb_range": _minmax("total_rss_mb"),
        "browser_rss_mb_median": _med("browser_rss_mb"),
        "wrapper_rss_mb_median": _med("wrapper_rss_mb"),
        "browser_procs_median": int(round(statistics.median([r["browser_procs"] for r in runs]))),
        "raw_runs": runs,
    }


def run_4way_heavy(
    chrome_bin: Path, moli_bin: Path, rust_mcp_bin: Path, origin: str, rounds: int, n_contexts: int
) -> Dict[str, Any]:
    stacks: List[Tuple[str, Callable[[], Dict[str, Any]]]] = [
        ("chrome_node_mcp", lambda: _run_heavy_chrome_node(origin, n_contexts)),
        ("chrome_rust_cdp", lambda: _run_heavy_chrome_rust(chrome_bin, rust_mcp_bin, origin, n_contexts)),
        ("moli_node_mcp", lambda: _run_heavy_moli_node(moli_bin, origin, n_contexts)),
        ("moli_rust_pipe", lambda: _run_heavy_moli_rust(moli_bin, rust_mcp_bin, origin, n_contexts)),
    ]
    results: Dict[str, List[Dict[str, Any]]] = {k: [] for k, _ in stacks}
    for r in range(1, rounds + 1):
        for name, fn in stacks:
            print(f"  [4-way round {r}/{rounds}] {name}...", file=sys.stderr, flush=True)
            res = fn()
            results[name].append(res)
            print(
                f"    -> {res['total_ms']} ms | total {res['total_rss_mb']} MB "
                f"(browser {res['browser_rss_mb']} + wrapper {res['wrapper_rss_mb']}) | "
                f"leaks={res['cross_context_leaks']}",
                file=sys.stderr,
                flush=True,
            )

    summary = {k: _summarize_runs(v) for k, v in results.items()}
    cn = summary["chrome_node_mcp"]
    cr = summary["chrome_rust_cdp"]
    mn = summary["moli_node_mcp"]
    mr = summary["moli_rust_pipe"]

    summary["deltas"] = {
        "pure_mcp_wrapper_on_chrome_speedup_x": round(cn["total_ms_median"] / max(0.1, cr["total_ms_median"]), 2),
        "pure_mcp_wrapper_on_chrome_rss_saved_mb": round(cn["total_rss_mb_median"] - cr["total_rss_mb_median"], 1),
        "pure_mcp_wrapper_on_chrome_rss_saved_pct": round(
            (1.0 - cr["total_rss_mb_median"] / max(0.1, cn["total_rss_mb_median"])) * 100.0, 1
        ),
        "rust_moli_vs_chrome_node_speedup_x": round(cn["total_ms_median"] / max(0.1, mr["total_ms_median"]), 2),
        "rust_moli_vs_chrome_node_rss_ratio_x": round(
            cn["total_rss_mb_median"] / max(0.1, mr["total_rss_mb_median"]), 2
        ),
        "rust_moli_vs_chrome_node_rss_saved_pct": round(
            (1.0 - mr["total_rss_mb_median"] / max(0.1, cn["total_rss_mb_median"])) * 100.0, 1
        ),
        "rust_moli_vs_moli_node_speedup_x": round(mn["total_ms_median"] / max(0.1, mr["total_ms_median"]), 2),
    }
    return summary


def _run_soak_on_client(
    label: str,
    client: MCPStdioClient,
    out_dir: Path,
    origin: str,
    cycles: int,
    rss_fn: Callable[[], Dict[str, Any]],
) -> Dict[str, Any]:
    url = f"{origin}/whoami"
    future_exp = time.time() + 7200
    state_file = out_dir / f".soak-{label}.json"
    reload_lat: List[float] = []
    nav_lat: List[float] = []
    checkpoints: Dict[str, Dict[str, Any]] = {}
    passed = 0
    stale = 0
    errors: List[str] = []

    checkpoint_set = {1, 10, 25, 50, 75, cycles}
    t_all = time.perf_counter()
    for i in range(1, cycles + 1):
        payload = {
            "cookies": [
                {
                    "name": "sid",
                    "value": f"soak_ck_{i}",
                    "domain": "127.0.0.1",
                    "path": "/",
                    "expires": future_exp,
                    "httpOnly": False,
                    "secure": False,
                    "sameSite": "Lax",
                }
            ],
            "origins": [
                {
                    "origin": origin,
                    "localStorage": [
                        {"name": "session_token", "value": f"soak_ls_{i}"},
                        {"name": "agent_id", "value": f"soak_{label}"},
                    ],
                }
            ],
        }
        state_file.write_text(json.dumps(payload), encoding="utf-8")
        os.chmod(state_file, 0o600)

        t0 = time.perf_counter()
        r1 = client.call_tool("browser_set_storage_state", {"filename": str(state_file)}, timeout=30.0)
        t1 = time.perf_counter()
        r2 = client.call_tool("browser_navigate", {"url": f"{url}?c={i}", "snapshot": False}, timeout=30.0)
        t2 = time.perf_counter()

        reload_lat.append((t1 - t0) * 1000.0)
        nav_lat.append((t2 - t1) * 1000.0)
        txt = _extract_mcp_text(r2)
        if (
            not r1.get("result", {}).get("isError")
            and not r2.get("result", {}).get("isError")
            and f"sid=soak_ck_{i}" in txt
            and f"soak_ls_{i}" in txt
        ):
            passed += 1
        else:
            stale += 1
            if len(errors) < 3:
                errors.append(f"c{i}: {txt[:140]}")

        if i in checkpoint_set:
            checkpoints[f"cycle_{i}"] = rss_fn()

    wall_ms = round((time.perf_counter() - t_all) * 1000.0, 1)
    c10 = checkpoints.get("cycle_10", checkpoints.get("cycle_1", {}))
    c_end = checkpoints[f"cycle_{cycles}"]
    return {
        "label": label,
        "cycles": cycles,
        "passed": passed,
        "stale_or_failed": stale,
        "wall_ms": wall_ms,
        "reload_p50_ms": round(pct(reload_lat, 50), 2),
        "reload_p95_ms": round(pct(reload_lat, 95), 2),
        "reload_p99_ms": round(pct(reload_lat, 99), 2),
        "nav_p50_ms": round(pct(nav_lat, 50), 2),
        "nav_p95_ms": round(pct(nav_lat, 95), 2),
        "checkpoints": checkpoints,
        "total_rss_delta_c10_to_end_mb": round(
            float(c_end.get("total_rss_mb", 0.0)) - float(c10.get("total_rss_mb", 0.0)), 1
        ),
        "wrapper_rss_delta_c10_to_end_mb": round(
            float(c_end.get("node_mcp_rss_mb", 0.0)) - float(c10.get("node_mcp_rss_mb", 0.0)), 1
        ),
        "sample_errors": errors,
    }


def run_soak_tests(
    chrome_bin: Path, moli_bin: Path, rust_mcp_bin: Path, origin: str, cycles: int
) -> Dict[str, Any]:
    out: Dict[str, Any] = {}

    # 1) Moli + Rust pipe
    print(f"  [soak] {cycles} cycles on moli + sessfork-mcp (Rust pipe)...", file=sys.stderr, flush=True)
    with tempfile.TemporaryDirectory(prefix="sf_soak_mr_") as tmp:
        out_dir = Path(tmp) / "out"
        out_dir.mkdir(mode=0o700)
        client = MCPStdioClient(str(rust_mcp_bin), [f"--moli={moli_bin}", f"--output-dir={out_dir}"], {})
        try:
            client.initialize()
            out["moli_rust_pipe"] = _run_soak_on_client(
                "moli_rust", client, out_dir, origin, cycles, lambda: _rust_moli_tree_rss(client.proc.pid)
            )
        finally:
            client.close()

    # 2) Chrome + Rust --cdp-url
    print(f"  [soak] {cycles} cycles on chrome-headless-shell + sessfork-mcp --cdp-url...", file=sys.stderr, flush=True)
    cdp_port = _free_loopback_port()
    with tempfile.TemporaryDirectory(prefix="sf_soak_cr_") as tmp:
        tmp_dir = Path(tmp)
        out_dir = tmp_dir / "out"
        out_dir.mkdir(mode=0o700)
        chrome_proc = _launch_chrome_cdp(chrome_bin, tmp_dir / "profile", cdp_port)
        client_c: Optional[MCPStdioClient] = None
        try:
            _wait_cdp_http(cdp_port)
            client_c = MCPStdioClient(
                str(rust_mcp_bin),
                [f"--cdp-url=http://127.0.0.1:{cdp_port}", f"--output-dir={out_dir}"],
                {},
            )
            client_c.initialize()

            def _rss_c() -> Dict[str, Any]:
                c_info = get_process_tree_breakdown(chrome_proc.pid)
                r_info = get_process_tree_breakdown(client_c.proc.pid)  # type: ignore[union-attr]
                return {
                    "browser_rss_mb": c_info["total_rss_mb"],
                    "browser_procs": c_info["total_procs"],
                    "node_mcp_rss_mb": r_info["total_rss_mb"],
                    "total_rss_mb": round(c_info["total_rss_mb"] + r_info["total_rss_mb"], 1),
                }

            out["chrome_rust_cdp"] = _run_soak_on_client("chrome_rust", client_c, out_dir, origin, cycles, _rss_c)
        finally:
            if client_c is not None:
                client_c.close()
            _terminate_proc(chrome_proc)

    return out


def run_lifecycle_proofs(
    chrome_bin: Path, moli_bin: Path, rust_mcp_bin: Path, origin: str
) -> Dict[str, Any]:
    out: Dict[str, Any] = {}

    # 1) SIGKILL hard-kill proof on sessfork-mcp + moli (Unix FD 3/4 pipe)
    print("  [lifecycle] SIGKILL hard-kill orphan proof (moli + Rust pipe)...", file=sys.stderr, flush=True)
    with tempfile.TemporaryDirectory(prefix="sf_sigkill_") as tmp:
        out_dir = Path(tmp) / "out"
        out_dir.mkdir(mode=0o700)
        client = MCPStdioClient(str(rust_mcp_bin), [f"--moli={moli_bin}", f"--output-dir={out_dir}"], {})
        try:
            client.initialize()
            for i in range(3):
                client.call_tool("browser_tabs", {"action": "new", "isolatedContext": f"kill-ctx-{i}"})
                client.call_tool("browser_navigate", {"url": f"{origin}/whoami?k={i}", "snapshot": False})
            children_before = _list_child_pids(client.proc.pid)
            t_kill = time.perf_counter()
            os.kill(client.proc.pid, signal.SIGKILL)
            client.proc.wait(timeout=5)
            # Poll until all child moli processes exit
            deadline = time.perf_counter() + 3.0
            exit_ms = None
            while time.perf_counter() < deadline:
                survivors = [p for p in children_before if _pid_alive(p)]
                if not survivors:
                    exit_ms = round((time.perf_counter() - t_kill) * 1000.0, 1)
                    break
                time.sleep(0.01)
            survivors = [p for p in children_before if _pid_alive(p)]
            out["sigkill_moli_pipe"] = {
                "child_pids_observed": len(children_before),
                "surviving_orphans": len(survivors),
                "child_exit_ms": exit_ms,
                "passed": len(children_before) >= 1 and len(survivors) == 0,
            }
        finally:
            if client.proc.poll() is None:
                client.proc.kill()

    # 2) External Chrome --cdp-url non-destruction + zero leaked targets (both browser_close and stdin EOF!)
    print("  [lifecycle] External Chrome --cdp-url detach & EOF cleanup proof...", file=sys.stderr, flush=True)
    cdp_port = _free_loopback_port()
    with tempfile.TemporaryDirectory(prefix="sf_cdp_life_") as tmp:
        tmp_dir = Path(tmp)
        out_dir = tmp_dir / "out"
        out_dir.mkdir(mode=0o700)
        chrome_proc = _launch_chrome_cdp(chrome_bin, tmp_dir / "profile", cdp_port)
        try:
            ver_before = _wait_cdp_http(cdp_port)
            targets_before = json.loads(
                urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json/list", timeout=1.0).read()
            )
            base_ids = {t.get("id") for t in targets_before}

            # 2a: Explicit browser_close
            c1 = MCPStdioClient(
                str(rust_mcp_bin), [f"--cdp-url=http://127.0.0.1:{cdp_port}", f"--output-dir={out_dir}"], {}
            )
            c1.initialize()
            for i in range(3):
                c1.call_tool("browser_tabs", {"action": "new", "isolatedContext": f"det-{i}"})
                c1.call_tool("browser_navigate", {"url": f"{origin}/whoami?d={i}", "snapshot": False})
            c1.call_tool("browser_close", {})
            c1.close()
            time.sleep(0.15)
            ver_after_close = _wait_cdp_http(cdp_port, timeout=2.0)
            targets_after_close = json.loads(
                urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json/list", timeout=1.0).read()
            )
            leaked_after_close = [t for t in targets_after_close if t.get("id") not in base_ids]

            # 2b: Stdin EOF exit WITHOUT calling browser_close (tests Drop impl on BrowserManager!)
            c2 = MCPStdioClient(
                str(rust_mcp_bin), [f"--cdp-url=http://127.0.0.1:{cdp_port}", f"--output-dir={out_dir}"], {}
            )
            c2.initialize()
            for i in range(2):
                c2.call_tool("browser_tabs", {"action": "new", "isolatedContext": f"eof-{i}"})
                c2.call_tool("browser_navigate", {"url": f"{origin}/whoami?e={i}", "snapshot": False})
            assert c2.proc.stdin is not None
            c2.proc.stdin.close()
            c2.proc.wait(timeout=5)
            time.sleep(0.15)
            ver_after_eof = _wait_cdp_http(cdp_port, timeout=2.0)
            targets_after_eof = json.loads(
                urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json/list", timeout=1.0).read()
            )
            leaked_after_eof = [t for t in targets_after_eof if t.get("id") not in base_ids]

            out["external_cdp_non_destruction"] = {
                "browser": ver_before.get("Browser"),
                "alive_after_browser_close": ver_after_close.get("Browser") == ver_before.get("Browser"),
                "leaked_targets_after_browser_close": len(leaked_after_close),
                "alive_after_stdin_eof": ver_after_eof.get("Browser") == ver_before.get("Browser"),
                "leaked_targets_after_stdin_eof": len(leaked_after_eof),
                "passed": (
                    ver_after_close.get("Browser") == ver_before.get("Browser")
                    and len(leaked_after_close) == 0
                    and ver_after_eof.get("Browser") == ver_before.get("Browser")
                    and len(leaked_after_eof) == 0
                ),
            }
        finally:
            _terminate_proc(chrome_proc)

    return out


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Extended 4-way benchmark, 100-cycle soak, and lifecycle proof.")
    parser.add_argument("--rounds", type=int, default=3, help="Measured rounds per stack for 4-way heavy compare.")
    parser.add_argument("--contexts", type=int, default=12, help="Isolated contexts for 4-way heavy compare.")
    parser.add_argument("--soak-cycles", type=int, default=100, help="Hot-reload cycles for soak test.")
    parser.add_argument("--json-out", type=Path, default=None, help="Optional JSON report path.")
    args = parser.parse_args(argv)

    chrome_bin = find_playwright_headless_shell()
    moli_bin = find_moli_binary()
    rust_mcp_bin = find_rust_mcp_binary()
    if not chrome_bin or not moli_bin or not rust_mcp_bin:
        print(
            f"Missing required binaries: chrome={chrome_bin}, moli={moli_bin}, rust_mcp={rust_mcp_bin}",
            file=sys.stderr,
        )
        return 2

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), LocalAuthHandler)
    server.daemon_threads = True
    http_port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{http_port}"

    try:
        print("[1/3] Running 4-way 12x Heavy-DOM benchmark...", file=sys.stderr, flush=True)
        four_way = run_4way_heavy(chrome_bin, moli_bin, rust_mcp_bin, origin, args.rounds, args.contexts)

        print(f"[2/3] Running {args.soak_cycles}-cycle hot-reload memory soak...", file=sys.stderr, flush=True)
        soak = run_soak_tests(chrome_bin, moli_bin, rust_mcp_bin, origin, args.soak_cycles)

        print("[3/3] Running SIGKILL & external CDP lifecycle proofs...", file=sys.stderr, flush=True)
        lifecycle = run_lifecycle_proofs(chrome_bin, moli_bin, rust_mcp_bin, origin)
    finally:
        server.shutdown()
        server.server_close()

    report = {
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "chrome_binary": chrome_bin.name,
            "moli_version": (get_moli_version(moli_bin) or "moli").strip(),
            "rust_mcp_version": (get_rust_mcp_version(rust_mcp_bin) or "sessfork-mcp").strip(),
            "rust_mcp_bytes": rust_mcp_bin.stat().st_size,
            "playwright_mcp_pkg": DEFAULT_PLAYWRIGHT_MCP_PKG,
        },
        "four_way_heavy_12x": four_way,
        "soak_100_cycles": soak,
        "lifecycle_proofs": lifecycle,
    }
    payload = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.json_out is not None:
        args.json_out.write_text(payload, encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
