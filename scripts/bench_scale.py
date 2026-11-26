#!/usr/bin/env python3
"""High-scale density ladder, multi-agent swarm concurrency, and 500-cycle soak harness.

Designed for 100% local execution on loopback 127.0.0.1 (zero external network, zero real
user browser data, zero OS Keychain prompts) with an automatic macOS memory-pressure circuit
breaker so high-scale runs never freeze or OOM-kill desktop applications.

Suites:
  1. Single-Host Concurrency Density Ladder (1,775 DOM nodes/tab, full AX tree, 0 leaks):
       - chrome_node_mcp: [12, 24, 48]
       - chrome_rust_cdp: [12, 24, 48]
       - moli_node_mcp:   [12, 24, 48, 96]
       - moli_rust_pipe:  [12, 24, 48, 96, 128]
     Computes marginal MB/context slope and estimated 24 GB / 32 GB node capacity.
  2. Multi-Agent Independent Process Swarm (W = 4, 8, 16 concurrent MCP servers):
       - chrome_node_mcp (W dedicated Node MCP + Chrome trees)
       - chrome_rust_cdp_pooled (W independent Rust MCP clients -> 1 shared Chrome daemon, isolated contexts)
       - moli_rust_pipe (W dedicated Rust MCP + Moli pipe processes)
     Measures peak aggregate RSS across all live workers (barrier-synchronized), throughput
     (tasks/sec), p50/p95/p99 tail latency, and 0 cross-agent leakage under multi-core load.
  3. 500-Cycle High-Frequency Hot-Reload Memory Soak:
       - moli_rust_pipe & chrome_rust_cdp
     Tracks RSS at c1/c25/c50/c100/c250/c500 to verify flat Rust wrapper footprint.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import http.server
import json
import os
import platform
import re
import socket
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

from bench_extended import (  # noqa: E402
    _free_loopback_port,
    _launch_chrome_cdp,
    _run_soak_on_client,
    _rust_moli_tree_rss,
    _terminate_proc,
    _wait_cdp_http,
)
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
    _parse_current_tab_index,
    _parse_page_title_line,
    get_process_tree_breakdown,
    pct,
)


def _host_memory_telemetry() -> Dict[str, Any]:
    info: Dict[str, Any] = {
        "total_ram_gb": None,
        "ncpu": os.cpu_count() or 0,
        "free_mem_pct": None,
        "swap_used_mb": None,
        "load_1m": None,
    }
    try:
        info["load_1m"] = round(os.getloadavg()[0], 2)
    except OSError:
        info["load_1m"] = None
    if platform.system() == "Darwin":
        try:
            mem_bytes = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True, timeout=3).strip())
            info["total_ram_gb"] = round(mem_bytes / (1024**3), 1)
        except (subprocess.SubprocessError, OSError, ValueError):
            info["total_ram_gb"] = None
        try:
            sw = subprocess.check_output(["sysctl", "-n", "vm.swapusage"], text=True, timeout=3)
            m = re.search(r"used\s*=\s*([0-9.]+)M", sw)
            if m:
                info["swap_used_mb"] = round(float(m.group(1)), 1)
        except (subprocess.SubprocessError, OSError, ValueError):
            info["swap_used_mb"] = None
        try:
            mp = subprocess.check_output(["memory_pressure", "-Q"], text=True, timeout=5)
            m = re.search(r"System-wide memory free percentage:\s*(\d+)%", mp)
            if m:
                info["free_mem_pct"] = int(m.group(1))
        except (subprocess.SubprocessError, OSError, ValueError):
            info["free_mem_pct"] = None
    return info


def _check_safe_memory(min_free_pct: int, stack_rss_mb: float, max_stack_rss_mb: float) -> Tuple[bool, str]:
    if stack_rss_mb >= max_stack_rss_mb:
        return False, f"stack RSS {stack_rss_mb:.1f} MB reached safety cap {max_stack_rss_mb:.0f} MB"
    tel = _host_memory_telemetry()
    free_pct = tel.get("free_mem_pct")
    if free_pct is not None and free_pct < min_free_pct:
        return False, f"macOS free memory {free_pct}% dropped below safety threshold {min_free_pct}%"
    return True, "ok"


def _run_guarded_heavy_workload(
    client: MCPStdioClient,
    out_dir: Path,
    origin: str,
    n_contexts: int,
    prefix: str,
    rss_fn: Callable[[], Dict[str, Any]],
    min_free_pct: int,
    max_stack_rss_mb: float,
) -> Dict[str, Any]:
    url = f"{origin}/heavy"
    future_exp = time.time() + 7200
    ctx_tab_indices: Dict[int, int] = {}
    setup_ok = 0
    leaks = 0
    nav_errors = 0
    sample_errors: List[str] = []
    observed_nodes = 0
    nav_lat_ms: List[float] = []
    aborted_reason: Optional[str] = None

    t0 = time.perf_counter()
    for idx in range(n_contexts):
        if idx > 0 and idx % 8 == 0:
            cur_mem = rss_fn()
            safe, reason = _check_safe_memory(min_free_pct, float(cur_mem["total_rss_mb"]), max_stack_rss_mb)
            if not safe:
                aborted_reason = f"circuit_breaker_at_ctx_{idx}: {reason}"
                break

        st = out_dir / f".{prefix}-s-{idx}.json"
        st.write_text(
            json.dumps(
                {
                    "cookies": [
                        {
                            "name": "sid",
                            "value": f"sc_u{idx}_ck",
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
                                {"name": "session_token", "value": f"sc_u{idx}_jwt"},
                                {"name": "agent_id", "value": f"sc_agent_{idx}"},
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        os.chmod(st, 0o600)

        tab_txt = _extract_mcp_text(
            client.call_tool("browser_tabs", {"action": "new", "isolatedContext": f"{prefix}-s-{idx}"}, timeout=60.0)
        )
        ctx_tab_indices[idx] = _parse_current_tab_index(tab_txt, idx + 1)
        client.call_tool("browser_set_storage_state", {"filename": str(st)}, timeout=60.0)
        tn0 = time.perf_counter()
        nav_resp = client.call_tool("browser_navigate", {"url": f"{url}?init={idx}"}, timeout=90.0)
        nav_txt = _extract_mcp_text(nav_resp)
        nav_lat_ms.append((time.perf_counter() - tn0) * 1000.0)
        tl = _parse_page_title_line(nav_txt)
        m_nodes = re.search(r"nodes=(\d+)", tl)
        if m_nodes and not observed_nodes:
            observed_nodes = int(m_nodes.group(1))
        foreign_setup = re.findall(r"sc_u(\d+)_(?:ck|jwt)", tl)
        has_foreign_setup = any(int(x) != idx for x in foreign_setup)
        if f"sid=sc_u{idx}_ck" in tl and f"sc_u{idx}_jwt" in tl and "rows=120" in tl and not has_foreign_setup:
            setup_ok += 1
        elif has_foreign_setup:
            leaks += 1
            if len(sample_errors) < 3:
                sample_errors.append(f"setup_leak_{idx}: {tl or nav_txt[:140]}")
        else:
            nav_errors += 1
            if len(sample_errors) < 3:
                sample_errors.append(f"setup_err_{idx}: {tl or nav_txt[:140]}")

    built = len(ctx_tab_indices)
    setup_ms = (time.perf_counter() - t0) * 1000.0
    verify_passed = 0
    verify_checks = 0
    t1 = time.perf_counter()

    if aborted_reason is None and built > 0:
        # Interleaved verification across up to 24 evenly distributed + edge contexts for large tiers,
        # or ALL contexts for <= 24 tiers, checking strict zero-leak isolation.
        if built <= 24:
            check_indices = list(reversed(range(built)))
        else:
            step = max(1, built // 24)
            check_indices = sorted(set(list(range(0, built, step)) + [0, built // 2, built - 1]), reverse=True)

        for idx in check_indices:
            verify_checks += 1
            client.call_tool("browser_tabs", {"action": "select", "index": ctx_tab_indices[idx]}, timeout=60.0)
            tn0 = time.perf_counter()
            chk_txt = _extract_mcp_text(
                client.call_tool("browser_navigate", {"url": f"{url}?verify={idx}"}, timeout=90.0)
            )
            nav_lat_ms.append((time.perf_counter() - tn0) * 1000.0)
            tl = _parse_page_title_line(chk_txt)
            # Check both exact match and zero foreign token substrings
            foreign_re = re.findall(r"sc_u(\d+)_(?:ck|jwt)", tl)
            has_foreign = any(int(x) != idx for x in foreign_re)
            if f"sid=sc_u{idx}_ck" in tl and f"sc_u{idx}_jwt" in tl and "rows=120" in tl and not has_foreign:
                verify_passed += 1
            elif has_foreign:
                leaks += 1
                if len(sample_errors) < 3:
                    sample_errors.append(f"verify_leak_{idx}: {tl or chk_txt[:140]}")
            else:
                nav_errors += 1
                if len(sample_errors) < 3:
                    sample_errors.append(f"verify_err_{idx}: {tl or chk_txt[:140]}")

    interleaved_ms = (time.perf_counter() - t1) * 1000.0
    total_ms = (time.perf_counter() - t0) * 1000.0
    final_mem = rss_fn()
    tel = _host_memory_telemetry()

    return {
        "target_contexts": n_contexts,
        "completed_contexts": built,
        "aborted_reason": aborted_reason,
        "dom_nodes_per_tab": observed_nodes,
        "total_live_dom_nodes": observed_nodes * built,
        "setup_verified": setup_ok,
        "verify_checks": verify_checks,
        "verify_passed": verify_passed,
        "nav_errors": nav_errors,
        "cross_context_leaks": leaks,
        "setup_ms": round(setup_ms, 1),
        "interleaved_ms": round(interleaved_ms, 1),
        "total_ms": round(total_ms, 1),
        "nav_p50_ms": round(pct(nav_lat_ms, 50), 1) if nav_lat_ms else 0.0,
        "nav_p95_ms": round(pct(nav_lat_ms, 95), 1) if nav_lat_ms else 0.0,
        "nav_p99_ms": round(pct(nav_lat_ms, 99), 1) if nav_lat_ms else 0.0,
        "browser_rss_mb": final_mem["browser_rss_mb"],
        "wrapper_rss_mb": final_mem["node_mcp_rss_mb"],
        "total_rss_mb": final_mem["total_rss_mb"],
        "browser_procs": final_mem["browser_procs"],
        "host_free_mem_pct_end": tel.get("free_mem_pct"),
        "host_swap_used_mb_end": tel.get("swap_used_mb"),
        "sample_errors": sample_errors,
    }


def _run_density_tier(
    stack: str,
    n_contexts: int,
    chrome_bin: Path,
    moli_bin: Path,
    rust_mcp_bin: Path,
    origin: str,
    min_free_pct: int,
    max_stack_rss_mb: float,
) -> Dict[str, Any]:
    safe, reason = _check_safe_memory(min_free_pct, 0.0, max_stack_rss_mb)
    if not safe:
        return {"target_contexts": n_contexts, "skipped_before_start": True, "aborted_reason": reason}

    if stack == "chrome_node_mcp":
        with tempfile.TemporaryDirectory(prefix="sf_sc_cn_") as tmp:
            st_file = Path(tmp) / "state.json"
            upsert_session_state([], [], state_path=st_file, replace_all=True)
            mirror = hot_reload_state_path(st_file)
            entry = build_playwright_mcp_entry(state_path=st_file)
            client = MCPStdioClient(entry["command"], entry["args"], entry["env"])
            try:
                client.initialize()
                return _run_guarded_heavy_workload(
                    client,
                    mirror.parent,
                    origin,
                    n_contexts,
                    "cn",
                    lambda: get_process_tree_breakdown(client.proc.pid),
                    min_free_pct,
                    max_stack_rss_mb,
                )
            finally:
                client.close()

    if stack == "chrome_rust_cdp":
        cdp_port = _free_loopback_port()
        with tempfile.TemporaryDirectory(prefix="sf_sc_cr_") as tmp:
            tmp_p = Path(tmp)
            out_dir = tmp_p / "out"
            out_dir.mkdir(mode=0o700)
            chrome_proc = _launch_chrome_cdp(chrome_bin, tmp_p / "profile", cdp_port)
            client_r: Optional[MCPStdioClient] = None
            try:
                _wait_cdp_http(cdp_port)
                client_r = MCPStdioClient(
                    str(rust_mcp_bin),
                    [f"--cdp-url=http://127.0.0.1:{cdp_port}", f"--output-dir={out_dir}"],
                    {},
                )
                client_r.initialize()

                def _rss() -> Dict[str, Any]:
                    c_i = get_process_tree_breakdown(chrome_proc.pid)
                    r_i = get_process_tree_breakdown(client_r.proc.pid)  # type: ignore[union-attr]
                    return {
                        "browser_rss_mb": c_i["total_rss_mb"],
                        "browser_procs": c_i["total_procs"],
                        "node_mcp_rss_mb": r_i["total_rss_mb"],
                        "total_rss_mb": round(c_i["total_rss_mb"] + r_i["total_rss_mb"], 1),
                    }

                return _run_guarded_heavy_workload(
                    client_r, out_dir, origin, n_contexts, "cr", _rss, min_free_pct, max_stack_rss_mb
                )
            finally:
                if client_r is not None:
                    client_r.close()
                _terminate_proc(chrome_proc)

    if stack == "moli_node_mcp":
        cdp_port = _free_loopback_port()
        cdp_url = f"http://127.0.0.1:{cdp_port}"
        with tempfile.TemporaryDirectory(prefix="sf_sc_mn_") as tmp:
            out_dir = Path(tmp) / "out"
            out_dir.mkdir(mode=0o700)
            moli_proc = subprocess.Popen(
                [str(moli_bin), "serve", "--host", "127.0.0.1", "--port", str(cdp_port)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            client_m: Optional[MCPStdioClient] = None
            try:
                _wait_cdp_http(cdp_port)
                npx = resolve_npx_command()
                client_m = MCPStdioClient(
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
                client_m.initialize()

                def _rss() -> Dict[str, Any]:
                    m_i = get_process_tree_breakdown(moli_proc.pid)
                    n_i = get_process_tree_breakdown(client_m.proc.pid)  # type: ignore[union-attr]
                    return {
                        "browser_rss_mb": m_i["total_rss_mb"],
                        "browser_procs": m_i["total_procs"],
                        "node_mcp_rss_mb": n_i["total_rss_mb"],
                        "total_rss_mb": round(m_i["total_rss_mb"] + n_i["total_rss_mb"], 1),
                    }

                return _run_guarded_heavy_workload(
                    client_m, out_dir, origin, n_contexts, "mn", _rss, min_free_pct, max_stack_rss_mb
                )
            finally:
                if client_m is not None:
                    client_m.close()
                _terminate_proc(moli_proc)

    # moli_rust_pipe
    with tempfile.TemporaryDirectory(prefix="sf_sc_mr_") as tmp:
        out_dir = Path(tmp) / "out"
        out_dir.mkdir(mode=0o700)
        client_mr = MCPStdioClient(str(rust_mcp_bin), [f"--moli={moli_bin}", f"--output-dir={out_dir}"], {})
        try:
            client_mr.initialize()
            return _run_guarded_heavy_workload(
                client_mr,
                out_dir,
                origin,
                n_contexts,
                "mr",
                lambda: _rust_moli_tree_rss(client_mr.proc.pid),
                min_free_pct,
                max_stack_rss_mb,
            )
        finally:
            client_mr.close()


def run_density_ladder(
    chrome_bin: Path,
    moli_bin: Path,
    rust_mcp_bin: Path,
    origin: str,
    min_free_pct: int,
    max_stack_rss_mb: float,
) -> Dict[str, Any]:
    plan = [
        ("moli_rust_pipe", [12, 24, 48]),
        ("moli_node_mcp", [12, 24, 48]),
        ("chrome_rust_cdp", [12, 24, 48]),
        ("chrome_node_mcp", [12, 24, 48]),
    ]
    out: Dict[str, Any] = {}
    for stack, tiers in plan:
        runs: List[Dict[str, Any]] = []
        for n in tiers:
            print(f"  [density] {stack} @ {n} contexts...", file=sys.stderr, flush=True)
            res = _run_density_tier(
                stack, n, chrome_bin, moli_bin, rust_mcp_bin, origin, min_free_pct, max_stack_rss_mb
            )
            runs.append(res)
            if res.get("aborted_reason") or res.get("skipped_before_start"):
                print(
                    f"    -> STOPPED ({res.get('aborted_reason')}); completed={res.get('completed_contexts', 0)} "
                    f"rss={res.get('total_rss_mb', 0)} MB",
                    file=sys.stderr,
                    flush=True,
                )
                break
            print(
                f"    -> {res['completed_contexts']}/{n} OK | {res['total_ms']} ms | "
                f"total {res['total_rss_mb']} MB (browser {res['browser_rss_mb']} + wrapper {res['wrapper_rss_mb']}) | "
                f"p95={res['nav_p95_ms']} ms | free={res['host_free_mem_pct_end']}% | leaks={res['cross_context_leaks']}",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(0.5)

        completed_runs = [
            r
            for r in runs
            if not r.get("aborted_reason")
            and not r.get("skipped_before_start")
            and r.get("nav_errors", 0) == 0
            and r.get("cross_context_leaks", 0) == 0
        ]
        slope_mb = None
        base_mb = None
        est_24gb_max = None
        est_32gb_max = None
        if len(completed_runs) >= 2:
            lo, hi = completed_runs[0], completed_runs[-1]
            dn = hi["completed_contexts"] - lo["completed_contexts"]
            if dn > 0:
                slope_mb = round((hi["total_rss_mb"] - lo["total_rss_mb"]) / dn, 2)
                base_mb = round(max(0.0, lo["total_rss_mb"] - slope_mb * lo["completed_contexts"]), 1)
                if slope_mb > 0:
                    # Conservative 75% usable budget on 24 GiB (18,432 MB) and 32 GiB (24,576 MB) nodes
                    est_24gb_max = int(max(0.0, (18432.0 - base_mb) / slope_mb))
                    est_32gb_max = int(max(0.0, (24576.0 - base_mb) / slope_mb))
        out[stack] = {
            "runs": runs,
            "marginal_mb_per_context": slope_mb,
            "estimated_base_mb": base_mb,
            "est_max_contexts_24gb_75pct_budget": est_24gb_max,
            "est_max_contexts_32gb_75pct_budget": est_32gb_max,
        }
    return out


def _run_swarm_level(
    stack: str,
    workers: int,
    tasks_per_worker: int,
    chrome_bin: Path,
    moli_bin: Path,
    rust_mcp_bin: Path,
    origin: str,
    min_free_pct: int,
    max_stack_rss_mb: float,
) -> Dict[str, Any]:
    safe, reason = _check_safe_memory(min_free_pct, 0.0, max_stack_rss_mb)
    if not safe:
        return {"workers": workers, "skipped": True, "reason": reason}

    url = f"{origin}/heavy"
    future_exp = time.time() + 7200
    shared_chrome_proc: Optional[subprocess.Popen[bytes]] = None
    shared_cdp_port: Optional[int] = None

    with tempfile.TemporaryDirectory(prefix=f"sf_sw_{stack}_{workers}_") as tmp:
        tmp_p = Path(tmp)
        if stack == "chrome_rust_cdp_pooled":
            shared_cdp_port = _free_loopback_port()
            shared_chrome_proc = _launch_chrome_cdp(chrome_bin, tmp_p / "shared_profile", shared_cdp_port)
            _wait_cdp_http(shared_cdp_port)

        barrier = threading.Barrier(workers)
        peak_rss_box: Dict[str, Any] = {}
        active_pids: List[int] = []
        pids_lock = threading.Lock()
        errors_lock = threading.Lock()
        sample_errors: List[str] = []

        def _worker(wid: int) -> Dict[str, Any]:
            w_dir = tmp_p / f"w{wid}"
            w_dir.mkdir(mode=0o700)
            client: Optional[MCPStdioClient] = None
            task_lats: List[float] = []
            passed = 0
            leaks = 0
            init_ms = 0.0
            try:
                t_start = time.perf_counter()
                if stack == "moli_rust_pipe":
                    out_dir = w_dir / "out"
                    out_dir.mkdir(mode=0o700)
                    client = MCPStdioClient(
                        str(rust_mcp_bin), [f"--moli={moli_bin}", f"--output-dir={out_dir}"], {}
                    )
                elif stack == "chrome_rust_cdp_pooled":
                    out_dir = w_dir / "out"
                    out_dir.mkdir(mode=0o700)
                    client = MCPStdioClient(
                        str(rust_mcp_bin),
                        [f"--cdp-url=http://127.0.0.1:{shared_cdp_port}", f"--output-dir={out_dir}"],
                        {},
                    )
                else:  # chrome_node_mcp
                    st_file = w_dir / "state.json"
                    upsert_session_state([], [], state_path=st_file, replace_all=True)
                    mirror = hot_reload_state_path(st_file)
                    out_dir = mirror.parent
                    entry = build_playwright_mcp_entry(state_path=st_file)
                    client = MCPStdioClient(entry["command"], entry["args"], entry["env"])

                client.initialize()
                init_ms = (time.perf_counter() - t_start) * 1000.0
                with pids_lock:
                    active_pids.append(client.proc.pid)

                # Each agent opens its own named isolated context and runs tasks_per_worker heavy navigations
                client.call_tool("browser_tabs", {"action": "new", "isolatedContext": f"sw-{wid}"}, timeout=90.0)
                st_path = out_dir / f".sw-{wid}.json"
                st_path.write_text(
                    json.dumps(
                        {
                            "cookies": [
                                {
                                    "name": "sid",
                                    "value": f"swarm_w{wid}_ck",
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
                                        {"name": "session_token", "value": f"swarm_w{wid}_jwt"},
                                        {"name": "agent_id", "value": f"swarm_{wid}"},
                                    ],
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                os.chmod(st_path, 0o600)
                client.call_tool("browser_set_storage_state", {"filename": str(st_path)}, timeout=90.0)

                for step in range(tasks_per_worker):
                    tn0 = time.perf_counter()
                    nav_txt = _extract_mcp_text(
                        client.call_tool("browser_navigate", {"url": f"{url}?w={wid}&s={step}"}, timeout=120.0)
                    )
                    task_lats.append((time.perf_counter() - tn0) * 1000.0)
                    tl = _parse_page_title_line(nav_txt)
                    foreign = re.findall(r"swarm_w(\d+)_(?:ck|jwt)", tl)
                    has_foreign = any(int(x) != wid for x in foreign)
                    if f"sid=swarm_w{wid}_ck" in tl and f"swarm_w{wid}_jwt" in tl and "rows=120" in tl and not has_foreign:
                        passed += 1
                    else:
                        leaks += 1
                        with errors_lock:
                            if len(sample_errors) < 3:
                                sample_errors.append(f"w{wid}s{step}: {tl or nav_txt[:140]}")

                # Synchronize ALL workers while loaded so worker 0 captures true aggregate live RSS!
                idx_at_barrier = barrier.wait(timeout=180.0)
                if idx_at_barrier == 0:
                    tot_b = tot_w = tot_procs = 0.0
                    with pids_lock:
                        pids_snap = list(active_pids)
                    if stack == "moli_rust_pipe":
                        for p in pids_snap:
                            info = _rust_moli_tree_rss(p)
                            tot_b += info["browser_rss_mb"]
                            tot_w += info["node_mcp_rss_mb"]
                            tot_procs += info["browser_procs"]
                    elif stack == "chrome_rust_cdp_pooled":
                        assert shared_chrome_proc is not None
                        c_i = get_process_tree_breakdown(shared_chrome_proc.pid)
                        tot_b = c_i["total_rss_mb"]
                        tot_procs = c_i["total_procs"]
                        for p in pids_snap:
                            r_i = get_process_tree_breakdown(p)
                            tot_w += r_i["total_rss_mb"]
                    else:
                        for p in pids_snap:
                            c_i = get_process_tree_breakdown(p)
                            tot_b += c_i["browser_rss_mb"]
                            tot_w += c_i["node_mcp_rss_mb"]
                            tot_procs += c_i["browser_procs"]
                    tel = _host_memory_telemetry()
                    peak_rss_box.update(
                        {
                            "browser_rss_mb": round(tot_b, 1),
                            "wrapper_rss_mb": round(tot_w, 1),
                            "total_rss_mb": round(tot_b + tot_w, 1),
                            "browser_procs": int(tot_procs),
                            "host_load_1m_peak": tel.get("load_1m"),
                            "host_free_mem_pct_peak": tel.get("free_mem_pct"),
                            "host_swap_used_mb_peak": tel.get("swap_used_mb"),
                        }
                    )
                barrier.wait(timeout=60.0)
            except Exception as exc:
                barrier.abort()
                with errors_lock:
                    if len(sample_errors) < 3:
                        sample_errors.append(f"w{wid}_exc: {type(exc).__name__}: {exc}")
            finally:
                if client is not None:
                    client.close()
            return {"init_ms": init_ms, "task_lats": task_lats, "passed": passed, "leaks": leaks}

        t_all = time.perf_counter()
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
                futs = [ex.submit(_worker, i) for i in range(workers)]
                worker_results = [f.result() for f in futs]
        finally:
            _terminate_proc(shared_chrome_proc)
        wall_ms = (time.perf_counter() - t_all) * 1000.0

    all_lats = [x for r in worker_results for x in r["task_lats"]]
    all_inits = [r["init_ms"] for r in worker_results if r["init_ms"] > 0]
    total_passed = sum(r["passed"] for r in worker_results)
    total_leaks = sum(r["leaks"] for r in worker_results)
    expected_tasks = workers * tasks_per_worker
    return {
        "workers": workers,
        "tasks_per_worker": tasks_per_worker,
        "expected_tasks": expected_tasks,
        "passed_tasks": total_passed,
        "cross_agent_leaks": total_leaks,
        "wall_ms": round(wall_ms, 1),
        "throughput_tasks_per_sec": round(total_passed / max(0.001, wall_ms / 1000.0), 2),
        "init_p50_ms": round(pct(all_inits, 50), 1) if all_inits else 0.0,
        "init_p95_ms": round(pct(all_inits, 95), 1) if all_inits else 0.0,
        "nav_p50_ms": round(pct(all_lats, 50), 1) if all_lats else 0.0,
        "nav_p95_ms": round(pct(all_lats, 95), 1) if all_lats else 0.0,
        "nav_p99_ms": round(pct(all_lats, 99), 1) if all_lats else 0.0,
        **peak_rss_box,
        "sample_errors": sample_errors,
    }


def run_swarm_suite(
    chrome_bin: Path,
    moli_bin: Path,
    rust_mcp_bin: Path,
    origin: str,
    min_free_pct: int,
    max_stack_rss_mb: float,
) -> Dict[str, Any]:
    stacks = [
        ("moli_rust_pipe", [4, 8, 16]),
        ("chrome_rust_cdp_pooled", [4, 8, 16]),
        ("chrome_node_mcp", [4, 8, 16]),
    ]
    out: Dict[str, Any] = {}
    for stack, levels in stacks:
        runs: List[Dict[str, Any]] = []
        for w in levels:
            print(f"  [swarm] {stack} @ {w} parallel agents x 3 heavy tasks...", file=sys.stderr, flush=True)
            res = _run_swarm_level(
                stack, w, 3, chrome_bin, moli_bin, rust_mcp_bin, origin, min_free_pct, max_stack_rss_mb
            )
            runs.append(res)
            print(
                f"    -> {res.get('passed_tasks', 0)}/{res.get('expected_tasks', w*3)} OK | "
                f"wall {res.get('wall_ms', 0)} ms ({res.get('throughput_tasks_per_sec', 0)} tasks/s) | "
                f"total {res.get('total_rss_mb', 0)} MB (browser {res.get('browser_rss_mb', 0)} + "
                f"wrapper {res.get('wrapper_rss_mb', 0)}) | p99={res.get('nav_p99_ms', 0)} ms | "
                f"load={res.get('host_load_1m_peak')} free={res.get('host_free_mem_pct_peak')}%",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(0.75)
            # Stop escalating this stack if peak memory hit the safety floor
            fp = res.get("host_free_mem_pct_peak")
            if (fp is not None and fp < min_free_pct) or float(res.get("total_rss_mb", 0.0)) >= max_stack_rss_mb:
                print(
                    f"    -> Circuit breaker tripped after {w} workers on {stack}; skipping higher concurrency.",
                    file=sys.stderr,
                    flush=True,
                )
                break
        out[stack] = runs
    return out


def run_soak_500(chrome_bin: Path, moli_bin: Path, rust_mcp_bin: Path, origin: str, cycles: int) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    print(f"  [soak-{cycles}] moli_rust_pipe...", file=sys.stderr, flush=True)
    with tempfile.TemporaryDirectory(prefix="sf_soak500_mr_") as tmp:
        out_dir = Path(tmp) / "out"
        out_dir.mkdir(mode=0o700)
        c = MCPStdioClient(str(rust_mcp_bin), [f"--moli={moli_bin}", f"--output-dir={out_dir}"], {})
        try:
            c.initialize()
            out["moli_rust_pipe"] = _run_soak_on_client(
                "moli500", c, out_dir, origin, cycles, lambda: _rust_moli_tree_rss(c.proc.pid)
            )
        finally:
            c.close()

    print(f"  [soak-{cycles}] chrome_rust_cdp...", file=sys.stderr, flush=True)
    cdp_port = _free_loopback_port()
    with tempfile.TemporaryDirectory(prefix="sf_soak500_cr_") as tmp:
        tmp_p = Path(tmp)
        out_dir = tmp_p / "out"
        out_dir.mkdir(mode=0o700)
        chrome_proc = _launch_chrome_cdp(chrome_bin, tmp_p / "profile", cdp_port)
        cc: Optional[MCPStdioClient] = None
        try:
            _wait_cdp_http(cdp_port)
            cc = MCPStdioClient(
                str(rust_mcp_bin), [f"--cdp-url=http://127.0.0.1:{cdp_port}", f"--output-dir={out_dir}"], {}
            )
            cc.initialize()

            def _rss() -> Dict[str, Any]:
                ci = get_process_tree_breakdown(chrome_proc.pid)
                ri = get_process_tree_breakdown(cc.proc.pid)  # type: ignore[union-attr]
                return {
                    "browser_rss_mb": ci["total_rss_mb"],
                    "browser_procs": ci["total_procs"],
                    "node_mcp_rss_mb": ri["total_rss_mb"],
                    "total_rss_mb": round(ci["total_rss_mb"] + ri["total_rss_mb"], 1),
                }

            out["chrome_rust_cdp"] = _run_soak_on_client("chrome500", cc, out_dir, origin, cycles, _rss)
        finally:
            if cc is not None:
                cc.close()
            _terminate_proc(chrome_proc)
    return out


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="High-scale density ladder, swarm concurrency, and 500-cycle soak.")
    parser.add_argument(
        "--min-free-pct", type=int, default=25, help="macOS free memory circuit breaker percent (default 25)."
    )
    parser.add_argument("--max-stack-rss-mb", type=float, default=14500.0, help="Per-stack RSS safety cap in MB.")
    parser.add_argument("--soak-cycles", type=int, default=500, help="Hot-reload soak cycles (default 500).")
    parser.add_argument("--json-out", type=Path, default=None, help="Optional JSON report path.")
    args = parser.parse_args(argv)

    chrome_bin = find_playwright_headless_shell()
    moli_bin = find_moli_binary()
    rust_mcp_bin = find_rust_mcp_binary()
    if not chrome_bin or not moli_bin or not rust_mcp_bin:
        print("Missing required binaries.", file=sys.stderr)
        return 2

    baseline = _host_memory_telemetry()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), LocalAuthHandler)
    server.daemon_threads = True
    http_port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{http_port}"

    try:
        print("[1/3] Running Single-Host Concurrency Density Ladder...", file=sys.stderr, flush=True)
        density = run_density_ladder(
            chrome_bin, moli_bin, rust_mcp_bin, origin, args.min_free_pct, args.max_stack_rss_mb
        )

        print("[2/3] Running Multi-Agent Independent Process Swarm (4 / 8 / 16 workers)...", file=sys.stderr, flush=True)
        swarm = run_swarm_suite(
            chrome_bin, moli_bin, rust_mcp_bin, origin, args.min_free_pct, args.max_stack_rss_mb
        )

        print(f"[3/3] Running {args.soak_cycles}-Cycle High-Frequency Soak...", file=sys.stderr, flush=True)
        soak = run_soak_500(chrome_bin, moli_bin, rust_mcp_bin, origin, args.soak_cycles)
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
            "playwright_mcp_pkg": DEFAULT_PLAYWRIGHT_MCP_PKG,
            "baseline_telemetry": baseline,
            "end_telemetry": _host_memory_telemetry(),
            "circuit_breaker": {
                "min_free_mem_pct": args.min_free_pct,
                "max_stack_rss_mb": args.max_stack_rss_mb,
            },
        },
        "density_ladder": density,
        "swarm_concurrency": swarm,
        "soak_500": soak,
    }
    payload = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.json_out is not None:
        args.json_out.write_text(payload, encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
