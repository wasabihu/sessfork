#!/usr/bin/env python3
"""100% local, reproducible small-model token & ergonomic benchmark for sessfork-mcp.

Compares 4 stacks head-to-head on the EXACT same authenticated multi-step SaaS Admin SPA
(127.0.0.1 loopback, zero external network, zero real browser credentials, zero API keys):

  1. chrome_node_mcp     : Chrome + Node @playwright/mcp (industry baseline)
  2. chrome_rust_default : Chrome + Rust sessfork-mcp --cdp-url (full YAML + Action Receipt)
  3. chrome_rust_ergo    : Chrome + Rust sessfork-mcp --cdp-url --compact --diff
  4. moli_rust_ergo      : Moli   + Rust sessfork-mcp --compact --diff (zero-layout)

Measures:
  - Upfront MCP `tools/list` schema tax (chars & ~tokens)
  - Per-step & total observation payload (chars & ~tokens)
  - Cumulative multi-turn billable input ~tokens across the full 8-step conversation
    (accounting for conversation-history compounding: turn k re-sends schema + turns 1..k)
  - Ergonomic small-model guardrails:
      * NO_OP detection on validation-gated/inert clicks (and 0-body omission)
      * Zero-layout active modal scoping (obscured background controls suppressed)
      * Persistent [ref=eN] stability across same-page DOM updates
      * 1-step `browser_type(..., submit=True)` navigation settle
  - Wall-clock execution time & peak RSS
"""

from __future__ import annotations

import argparse
import http.server
import json
import platform
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from bench_extended import (  # noqa: E402
    _free_loopback_port,
    _launch_chrome_cdp,
    _rust_moli_tree_rss,
    _terminate_proc,
    _wait_cdp_http,
)
from sessfork.mcp_setup import (  # noqa: E402
    DEFAULT_PLAYWRIGHT_MCP_PKG,
    build_playwright_mcp_entry,
    find_playwright_headless_shell,
)
from sessfork.moli_bridge import (  # noqa: E402
    find_moli_binary,
    find_rust_mcp_binary,
    get_moli_version,
    get_rust_mcp_version,
)
from sessfork.state import hot_reload_state_path, upsert_session_state  # noqa: E402
from stress_test import (  # noqa: E402
    MCPStdioClient,
    _extract_mcp_text,
    get_process_tree_breakdown,
)


def _approx_tokens(text: str) -> int:
    """Standard conservative char/4 token estimate (comparable across tokenizers)."""
    return max(1, (len(text) + 3) // 4) if text else 0


class _AdminSpaHandler(http.server.BaseHTTPRequestHandler):
    """Realistic authenticated SaaS Admin SPA (~1,500+ DOM nodes, modal, validation trap)."""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        cookie = self.headers.get("Cookie", "")
        has_auth_cookie = "admin_sid=VALID_SESSION_TOKEN" in cookie

        if parsed.path == "/admin/orders":
            sidebar_links = "".join(
                f'<li><a href="/admin/section-{i}">Workspace Section {i}: Analytics &amp; Audit Reports</a></li>'
                for i in range(1, 16)
            )
            help_cards = "".join(
                f"<article><h3>Policy Notice #{i}</h3>"
                f"<p>All payouts above $1,000 require reviewer verification, tax reconciliation, "
                f"and compliance audit trail attachment before batch export is unlocked.</p></article>"
                for i in range(1, 13)
            )
            order_rows = []
            for idx in range(9001, 9046):
                status = "Pending Review" if idx == 9042 else ("Paid" if idx % 2 == 0 else "Settled")
                amount = "$4,200.00" if idx == 9042 else f"${120 + (idx % 37) * 15}.00"
                order_rows.append(
                    f'<tr data-order="ORD-{idx}">'
                    f"<td>ORD-{idx}</td>"
                    f"<td>Acme Enterprise Account #{idx % 19}</td>"
                    f"<td>{status}</td>"
                    f"<td>{amount}</td>"
                    f"<td>2026-11-{(idx % 25) + 1:02d}</td>"
                    f'<td><a href="/admin/detail?id=ORD-{idx}">Inspect ORD-{idx}</a></td>'
                    f"</tr>"
                )
            rows_html = "".join(order_rows)
            auth_flag = "true" if has_auth_cookie else "false"
            html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Admin Orders Console</title></head>
<body>
<header>
  <h1>Enterprise Payout &amp; Orders Console</h1>
  <div id="auth-badge">AUTH_CHECKING</div>
</header>
<nav aria-label="Sidebar"><ul>{sidebar_links}</ul></nav>
<main>
  <section aria-label="Policy Notes">{help_cards}</section>
  <section aria-label="Toolbar">
    <input id="filter-input" type="text" placeholder="Filter Order ID" />
    <button id="apply-filter" type="button" onclick="applyFilter()">Apply Filter</button>
    <button id="approve-btn" type="button" onclick="openConfirmModal()">Approve Payout</button>
    <button id="export-btn" type="button" onclick="handleExport()">Export Approved CSV</button>
  </section>
  <form id="jump-form" action="/admin/receipt" method="get">
    <input id="jump-input" name="id" type="text" placeholder="Quick Jump Receipt ID" />
  </form>
  <div id="status-slot"></div>
  <div id="modal-slot"></div>
  <table>
    <thead><tr><th>Order</th><th>Customer</th><th>Status</th><th>Amount</th><th>Date</th><th>Link</th></tr></thead>
    <tbody id="orders-body">{rows_html}</tbody>
  </table>
</main>
<script>
const hasCookie = {auth_flag};
const hasJwt = localStorage.getItem('admin_jwt') === 'VALID_JWT_9042';
document.getElementById('auth-badge').textContent = (hasCookie && hasJwt) ? 'AUTH_OK_9042' : 'AUTH_MISSING';

let approved = false;

function applyFilter() {{
  const q = (document.getElementById('filter-input').value || '').trim().toUpperCase();
  const rows = document.querySelectorAll('#orders-body tr');
  let shown = 0;
  rows.forEach((r) => {{
    const keep = !q || (r.getAttribute('data-order') || '').includes(q);
    r.style.display = keep ? '' : 'none';
    if (!keep) r.setAttribute('hidden', 'hidden');
    else r.removeAttribute('hidden');
    if (keep) shown++;
  }});
  document.getElementById('status-slot').innerHTML =
    '<div role="status">FILTERED_ROWS=' + shown + ' QUERY=' + q + '</div>';
}}

function handleExport() {{
  // Realistic React/SPA validation gate: silently ignores click before payout approval.
  if (!approved) return;
  setTimeout(() => {{
    document.getElementById('status-slot').innerHTML =
      '<div role="status">EXPORT_READY_CSV_9042</div>';
  }}, 80);
}}

function openConfirmModal() {{
  document.getElementById('modal-slot').innerHTML =
    '<div role="alertdialog" aria-modal="true" aria-label="Confirm Payout $4,200">' +
    '<h2>Confirm Payout $4,200</h2>' +
    '<p>Approve wire payout for ORD-9042?</p>' +
    '<button id="modal-cancel" type="button" onclick="closeModal()">Cancel Payout</button>' +
    '<button id="modal-confirm" type="button" onclick="confirmPayout()">Confirm Wire</button>' +
    '</div>';
}}

function closeModal() {{
  document.getElementById('modal-slot').innerHTML = '';
}}

function confirmPayout() {{
  approved = true;
  document.getElementById('modal-slot').innerHTML = '';
  document.getElementById('status-slot').innerHTML =
    '<div role="status">PAYOUT_CONFIRMED_9042</div>';
}}
</script>
</body></html>"""
        elif parsed.path == "/admin/receipt":
            time.sleep(0.25)
            qs = parse_qs(parsed.query)
            oid = (qs.get("id") or ["UNKNOWN"])[0]
            html = (
                f"<!doctype html><html><head><title>Receipt {oid}</title></head>"
                f"<body><h1>Receipt Verified: {oid}</h1>"
                f'<div role="status">RECEIPT_VERIFIED_{oid}</div></body></html>'
            )
        else:
            html = "<!doctype html><html><head><title>Not Found</title></head><body>404</body></html>"

        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _find_ref(text: str, pattern: str) -> str:
    m = re.search(pattern, text)
    if not m:
        raise RuntimeError(f"Could not find pattern {pattern!r} in snapshot:\n{text[:1500]}")
    return m.group(1)


def _inspect_tool_arg_names(tools_resp: Dict[str, Any]) -> Tuple[int, int, str, str]:
    tools = tools_resp.get("result", {}).get("tools", [])
    schema_json = json.dumps(tools, separators=(",", ":"))
    click_key = "ref"
    type_key = "ref"
    for t in tools:
        name = t.get("name")
        props = t.get("inputSchema", {}).get("properties", {})
        if name == "browser_click":
            click_key = "ref" if "ref" in props else "target"
        elif name == "browser_type":
            type_key = "ref" if "ref" in props else "target"
    return len(tools), len(schema_json), click_key, type_key


def _list_tools(client: MCPStdioClient) -> Dict[str, Any]:
    client._id += 1  # noqa: SLF001
    req_id = client._id  # noqa: SLF001
    client._send({"jsonrpc": "2.0", "id": req_id, "method": "tools/list", "params": {}})  # noqa: SLF001
    return client._recv(req_id, timeout=30.0)  # noqa: SLF001


def _run_workflow(
    client: MCPStdioClient,
    origin: str,
    stack_name: str,
    rss_fn: Any,
    state_file_for_reload: Optional[Path] = None,
) -> Dict[str, Any]:
    t0 = time.perf_counter()
    tools_resp = _list_tools(client)
    n_tools, schema_chars, click_key, type_key = _inspect_tool_arg_names(tools_resp)
    schema_tokens = _approx_tokens("x" * schema_chars)

    if state_file_for_reload is not None:
        client.call_tool("browser_set_storage_state", {"filename": str(state_file_for_reload)})

    steps: List[Dict[str, Any]] = []
    peak_rss_mb = 0.0
    extra_file_reads = 0

    def _resolve_playwright_spill(txt: str) -> Tuple[str, int]:
        """Playwright MCP 0.0.68+ spills large snapshots to a .yml file that the agent must read."""
        m = re.search(r"- \[Snapshot\]\(([^)]+\.yml)\)", txt)
        if not m:
            return txt, 0
        raw_path = m.group(1)
        cand = Path(raw_path)
        if not cand.is_absolute():
            cand = (Path.cwd() / raw_path).resolve()
        if cand.is_file():
            spilled = cand.read_text(encoding="utf-8", errors="replace")
            return txt + "\n" + spilled, 1
        return txt, 0

    def _record(step_name: str, tool_name: str, args: Dict[str, Any]) -> str:
        nonlocal peak_rss_mb, extra_file_reads
        s_t0 = time.perf_counter()
        resp = client.call_tool(tool_name, args, timeout=30.0)
        elapsed_ms = (time.perf_counter() - s_t0) * 1000.0
        raw_txt = _extract_mcp_text(resp)
        txt, spilled = _resolve_playwright_spill(raw_txt)
        extra_file_reads += spilled
        rss = rss_fn().get("total_rss_mb", 0.0)
        if rss > peak_rss_mb:
            peak_rss_mb = rss
        steps.append(
            {
                "step": step_name,
                "tool": tool_name,
                "chars": len(txt),
                "approx_tokens": _approx_tokens(txt),
                "spilled_to_file": bool(spilled),
                "elapsed_ms": round(elapsed_ms, 1),
            }
        )
        return txt

    def _click_args(ref: str, label: str) -> Dict[str, Any]:
        return {click_key: ref, "element": label}

    def _type_args(ref: str, label: str, text: str, submit: bool = False) -> Dict[str, Any]:
        a: Dict[str, Any] = {type_key: ref, "text": text, "submit": submit}
        if type_key == "target":
            a["element"] = label
        return a

    # Step 1: Navigate to authenticated /admin/orders
    s1 = _record("1_navigate_admin", "browser_navigate", {"url": f"{origin}/admin/orders"})
    ref_export = _find_ref(s1, r'button "Export Approved CSV"[^\n]*\[ref=(e\d+)\]')
    ref_filter = _find_ref(s1, r'textbox "Filter Order ID"[^\n]*\[ref=(e\d+)\]')
    ref_apply = _find_ref(s1, r'button "Apply Filter"[^\n]*\[ref=(e\d+)\]')
    ref_approve = _find_ref(s1, r'button "Approve Payout"[^\n]*\[ref=(e\d+)\]')
    ref_jump = _find_ref(s1, r'textbox "Quick Jump Receipt ID"[^\n]*\[ref=(e\d+)\]')

    # Step 2: Click validation-gated Export button BEFORE approval (NO_OP trap!)
    s2 = _record("2_click_noop_trap", "browser_click", _click_args(ref_export, "Export Approved CSV"))
    caught_noop = "WARNING: NO_OP" in s2

    # Step 3: Type "ORD-9042" into Filter Order ID
    s3 = _record(
        "3_type_filter",
        "browser_type",
        _type_args(ref_filter, "Filter Order ID", "ORD-9042", submit=False),
    )
    if "### Snapshot" in s3 and "Apply Filter" in s3:
        ref_apply = _find_ref(s3, r'button "Apply Filter"[^\n]*\[ref=(e\d+)\]')

    # Step 4: Click Apply Filter (reduces 45 rows -> 1 row + status alert)
    s4 = _record("4_click_apply_filter", "browser_click", _click_args(ref_apply, "Apply Filter"))
    if "### Snapshot" in s4 and "Approve Payout" in s4:
        ref_approve = _find_ref(s4, r'button "Approve Payout"[^\n]*\[ref=(e\d+)\]')

    # Step 5: Click Approve Payout -> opens alertdialog modal
    s5 = _record("5_open_modal", "browser_click", _click_args(ref_approve, "Approve Payout"))
    modal_scoped = ("Active Modal:" in s5 or "Modal opened:" in s5) and ("Export Approved CSV" not in s5)
    ref_confirm = _find_ref(s5, r'button "Confirm Wire"[^\n]*\[ref=(e\d+)\]')

    # Step 6: Confirm Wire inside modal -> closes modal, unlocks Export
    s6 = _record("6_confirm_modal", "browser_click", _click_args(ref_confirm, "Confirm Wire"))
    if "Export Approved CSV" in s6:
        ref_export = _find_ref(s6, r'button "Export Approved CSV"[^\n]*\[ref=(e\d+)\]')
    if "Quick Jump Receipt ID" in s6:
        ref_jump = _find_ref(s6, r'textbox "Quick Jump Receipt ID"[^\n]*\[ref=(e\d+)\]')

    # Step 7: Click Export Approved CSV (now enabled -> async 80ms EXPORT_READY_CSV_9042)
    s7 = _record("7_click_export_ok", "browser_click", _click_args(ref_export, "Export Approved CSV"))
    if "Quick Jump Receipt ID" in s7:
        ref_jump = _find_ref(s7, r'textbox "Quick Jump Receipt ID"[^\n]*\[ref=(e\d+)\]')

    # Step 8: Type ORD-9042 with submit=True -> settles 250ms navigation to /admin/receipt
    s8 = _record(
        "8_type_submit_jump",
        "browser_type",
        _type_args(ref_jump, "Quick Jump Receipt ID", "ORD-9042", submit=True),
    )
    submit_settled_one_call = "RECEIPT_VERIFIED_ORD-9042" in s8 or "Receipt ORD-9042" in s8

    wall_ms = (time.perf_counter() - t0) * 1000.0
    total_obs_chars = sum(st["chars"] for st in steps)
    total_obs_tokens = sum(st["approx_tokens"] for st in steps)

    # Multi-turn conversation compounding: on turn k (1..N), the model re-reads
    # the upfront MCP tool schema + all prior observations 1..k.
    cumulative_input_tokens = 0
    running_hist = 0
    for st in steps:
        running_hist += st["approx_tokens"]
        cumulative_input_tokens += schema_tokens + running_hist

    return {
        "stack": stack_name,
        "tools_count": n_tools,
        "schema_chars": schema_chars,
        "schema_approx_tokens": schema_tokens,
        "steps": steps,
        "total_obs_chars": total_obs_chars,
        "total_obs_approx_tokens": total_obs_tokens,
        "extra_file_reads_required": extra_file_reads,
        "cumulative_multiturn_input_tokens": cumulative_input_tokens,
        "wall_ms": round(wall_ms, 1),
        "peak_rss_mb": round(peak_rss_mb, 1),
        "signals": {
            "caught_no_op_warning": caught_noop,
            "noop_step_chars": steps[1]["chars"],
            "modal_background_suppressed": modal_scoped,
            "modal_step_chars": steps[4]["chars"],
            "submit_settled_in_1_call": submit_settled_one_call,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Reproducible small-model token & ergonomic benchmark")
    parser.add_argument("--json-out", type=Path, default=None, help="Optional JSON output path")
    args = parser.parse_args()

    local_bin = REPO_ROOT / "rust" / "sessfork-mcp" / "target" / "release" / "sessfork-mcp"
    rust_mcp_bin = local_bin if local_bin.is_file() else find_rust_mcp_binary()
    moli_bin = find_moli_binary()
    chrome_bin = find_playwright_headless_shell()

    if not rust_mcp_bin or not moli_bin or not chrome_bin:
        print(
            f"Missing binary: rust_mcp={rust_mcp_bin}, moli={moli_bin}, chrome={chrome_bin}",
            file=sys.stderr,
        )
        return 1

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _AdminSpaHandler)
    srv.daemon_threads = True
    port = srv.server_address[1]
    origin = f"http://127.0.0.1:{port}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    exp = time.time() + 7200
    cookies = [
        {
            "name": "admin_sid",
            "value": "VALID_SESSION_TOKEN",
            "domain": "127.0.0.1",
            "path": "/",
            "expires": exp,
            "httpOnly": True,
            "secure": False,
            "sameSite": "Lax",
        }
    ]
    origins = [
        {
            "origin": origin,
            "localStorage": [{"name": "admin_jwt", "value": "VALID_JWT_9042"}],
        }
    ]

    results: List[Dict[str, Any]] = []
    try:
        # 1. Chrome + Node @playwright/mcp
        with tempfile.TemporaryDirectory(prefix="sf_tok_cn_") as tmp:
            st_file = Path(tmp) / "state.json"
            upsert_session_state(cookies, origins, state_path=st_file, replace_all=True)
            entry = build_playwright_mcp_entry(state_path=st_file)
            c = MCPStdioClient(entry["command"], entry["args"], entry["env"])
            try:
                c.initialize()
                results.append(
                    _run_workflow(
                        c,
                        origin,
                        "chrome_node_mcp",
                        lambda: get_process_tree_breakdown(c.proc.pid),
                    )
                )
            finally:
                c.close()

        # 2 & 3. Chrome + Rust --cdp-url (default vs --compact --diff)
        for stack_name, extra_flags in [
            ("chrome_rust_default", []),
            ("chrome_rust_compact_diff", ["--compact", "--diff"]),
        ]:
            cdp_port = _free_loopback_port()
            with tempfile.TemporaryDirectory(prefix=f"sf_tok_{stack_name}_") as tmp:
                tmp_p = Path(tmp)
                st_file = tmp_p / "state.json"
                upsert_session_state(cookies, origins, state_path=st_file, replace_all=True)
                mirror = hot_reload_state_path(st_file)
                chrome_proc = _launch_chrome_cdp(chrome_bin, tmp_p / "profile", cdp_port)
                c_r: Optional[MCPStdioClient] = None
                try:
                    _wait_cdp_http(cdp_port)
                    c_r = MCPStdioClient(
                        str(rust_mcp_bin),
                        [
                            f"--cdp-url=http://127.0.0.1:{cdp_port}",
                            f"--output-dir={mirror.parent}",
                            *extra_flags,
                        ],
                        {},
                    )
                    c_r.initialize()

                    def _chrome_rust_rss() -> Dict[str, Any]:
                        ci = get_process_tree_breakdown(chrome_proc.pid)
                        ri = get_process_tree_breakdown(c_r.proc.pid)  # type: ignore[union-attr]
                        return {"total_rss_mb": round(ci["total_rss_mb"] + ri["total_rss_mb"], 1)}

                    results.append(
                        _run_workflow(
                            c_r,
                            origin,
                            stack_name,
                            _chrome_rust_rss,
                            state_file_for_reload=mirror,
                        )
                    )
                finally:
                    if c_r is not None:
                        c_r.close()
                    _terminate_proc(chrome_proc)

        # 4. Moli + Rust Pipe (--compact --diff, zero-layout!)
        with tempfile.TemporaryDirectory(prefix="sf_tok_moli_") as tmp:
            tmp_p = Path(tmp)
            st_file = tmp_p / "state.json"
            upsert_session_state(cookies, origins, state_path=st_file, replace_all=True)
            mirror = hot_reload_state_path(st_file)
            c_m = MCPStdioClient(
                str(rust_mcp_bin),
                [
                    f"--moli={moli_bin}",
                    f"--output-dir={mirror.parent}",
                    "--compact",
                    "--diff",
                ],
                {},
            )
            try:
                c_m.initialize()
                results.append(
                    _run_workflow(
                        c_m,
                        origin,
                        "moli_rust_compact_diff",
                        lambda: _rust_moli_tree_rss(c_m.proc.pid),
                        state_file_for_reload=mirror,
                    )
                )
            finally:
                c_m.close()
    finally:
        srv.shutdown()
        srv.server_close()

    base = results[0]
    report = {
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "rust_mcp_version": get_rust_mcp_version(rust_mcp_bin),
            "moli_version": get_moli_version(moli_bin),
            "playwright_mcp_pkg": DEFAULT_PLAYWRIGHT_MCP_PKG,
        },
        "results": results,
    }

    print("\n=== 8-Step Authenticated SaaS Admin SPA — Token & Ergonomic Benchmark ===")
    header = (
        f"{'Stack':26s} | {'Schema':>7s} | {'8-Step Obs':>10s} | {'Cumul In':>9s} | "
        f"{'Saved':>6s} | {'Wall':>7s} | {'RSS':>7s} | {'NO_OP':>5s} | {'Modal':>5s} | {'Sub1':>4s}"
    )
    print(header)
    print("-" * len(header))
    for r in results:
        saved_pct = (1.0 - r["cumulative_multiturn_input_tokens"] / base["cumulative_multiturn_input_tokens"]) * 100.0
        sig = r["signals"]
        print(
            f"{r['stack']:26s} | "
            f"{r['schema_approx_tokens']:>5d}tk | "
            f"{r['total_obs_approx_tokens']:>8d}tk | "
            f"{r['cumulative_multiturn_input_tokens']:>7d}tk | "
            f"{saved_pct:>+5.1f}% | "
            f"{r['wall_ms']:>5.0f}ms | "
            f"{r['peak_rss_mb']:>5.1f}MB | "
            f"{('YES' if sig['caught_no_op_warning'] else 'no'):>5s} | "
            f"{('YES' if sig['modal_background_suppressed'] else 'no'):>5s} | "
            f"{('YES' if sig['submit_settled_in_1_call'] else 'no'):>4s}"
        )

    if args.json_out:
        args.json_out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nSaved JSON report -> {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
