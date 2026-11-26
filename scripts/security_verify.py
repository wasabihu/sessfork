#!/usr/bin/env python3
"""Runnable adversarial security verification harness for sessfork & sessfork-mcp.

Executes live black-box & white-box security probes against the real built `sessfork-mcp`
binary and Python `sessfork` modules in isolated temporary directories on loopback 127.0.0.1.

Zero external network requests, zero real user credentials, zero OS Keychain prompts.
Exits 0 if and only if every security invariant passes.
"""

from __future__ import annotations

import argparse
import http.client
import http.server
import json
import os
import platform
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from sessfork.chrome import BrowserProfileError, normalize_domain_filter  # noqa: E402
from sessfork.moli_bridge import (  # noqa: E402
    find_moli_binary,
    find_rust_mcp_binary,
    get_moli_version,
    get_rust_mcp_version,
)
from sessfork.state import upsert_session_state  # noqa: E402
from sessfork.ui_server import create_ui_server  # noqa: E402
from stress_test import MCPStdioClient, _extract_mcp_text  # noqa: E402


class SecurityAuditRunner:
    def __init__(self) -> None:
        self.checks: List[Dict[str, Any]] = []

    def record(self, domain: str, name: str, passed: bool, detail: str) -> None:
        self.checks.append(
            {
                "domain": domain,
                "check": name,
                "status": "PASS" if passed else "FAIL",
                "detail": detail,
            }
        )
        tag = "PASS" if passed else "FAIL"
        print(f"  [{tag}] {domain} :: {name} — {detail}", file=sys.stderr, flush=True)

    def run_check(self, domain: str, name: str, fn: Callable[[], Tuple[bool, str]]) -> None:
        try:
            ok, detail = fn()
        except Exception as exc:
            ok, detail = False, f"Unhandled exception: {type(exc).__name__}: {exc}"
        self.record(domain, name, ok, detail)


def _send_native_msg(rust_mcp_bin: Path, payload: Dict[str, Any], home_dir: Path) -> Tuple[int, Dict[str, Any], bytes]:
    raw = json.dumps(payload).encode("utf-8")
    framed = struct.pack("<I", len(raw)) + raw
    env = dict(os.environ)
    env["HOME"] = str(home_dir)
    proc = subprocess.run(
        [str(rust_mcp_bin), "native-host"],
        input=framed,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        timeout=10,
    )
    out = proc.stdout
    if len(out) < 4:
        return proc.returncode, {"_raw_stdout_len": len(out), "_stderr": proc.stderr.decode("utf-8", "replace")}, out
    resp_len = struct.unpack("<I", out[:4])[0]
    resp_bytes = out[4 : 4 + resp_len]
    return proc.returncode, json.loads(resp_bytes.decode("utf-8")), resp_bytes


def verify_d1_ssrf_cdp_allowlist(runner: SecurityAuditRunner, rust_mcp_bin: Path) -> None:
    domain = "D1_ssrf_cdp_allowlist"
    vectors = [
        ("cloud_metadata_imds", "http://169.254.169.254:80"),
        ("rfc1918_lan_http", "http://10.0.0.1:9222"),
        ("rfc1918_home_http", "http://192.168.1.1:9222"),
        ("public_host_http", "http://evil.com:9222"),
        ("public_host_ws", "ws://evil.com:9222/devtools/browser/1"),
        ("userinfo_at_spoof", "http://127.0.0.1@evil.com:9222"),
        ("userinfo_creds_loopback", "http://user:pass@127.0.0.1:9222"),
        ("wildcard_0_0_0_0", "http://0.0.0.0:9222"),
        ("zero_port", "http://127.0.0.1:0"),
        ("https_disallowed", "https://127.0.0.1:9222"),
        ("wss_disallowed", "wss://127.0.0.1:9222/devtools/browser/1"),
        ("file_scheme", "file:///etc/passwd"),
        ("http_extra_path", "http://127.0.0.1:9222/json/list"),
        ("url_fragment", "http://127.0.0.1:9222#frag"),
    ]
    blocked = 0
    failures: List[str] = []
    for label, url in vectors:
        p = subprocess.run(
            [str(rust_mcp_bin), f"--cdp-url={url}"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
        )
        if p.returncode == 2 and b"sessfork-mcp error:" in p.stderr:
            blocked += 1
        else:
            failures.append(f"{label}(rc={p.returncode})")
    runner.record(
        domain,
        "reject_all_14_ssrf_and_malformed_cdp_urls",
        blocked == len(vectors),
        f"{blocked}/{len(vectors)} fail-closed with exit code 2"
        + (f"; missed={failures}" if failures else ""),
    )


def verify_d2_d3_d4_d6_live_mcp(runner: SecurityAuditRunner, rust_mcp_bin: Path, moli_bin: Path) -> None:
    # Two distinct loopback origins: Origin A is hostile (overwrites JSON.stringify & Object.prototype)
    class HostileOriginAHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            html = """<!doctype html><html><head><title>OriginA</title></head><body>
<pre id="res"></pre>
<script>
// Hostile page attempts to tamper with global JSON and Object.prototype!
window.JSON.stringify = function() { return "HIJACKED_JSON"; };
window.JSON.parse = function() { return { hijacked: true }; };
Object.prototype.polluted = "pwned";
function dump() {
  var a = window.localStorage.getItem('secret_a') || '';
  var b = window.localStorage.getItem('secret_b') || '';
  document.title = 'ORIGA:' + a + '|LEAKB:' + b;
}
dump();
</script></body></html>"""
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    class BenignOriginBHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            html = """<!doctype html><html><head><title>OriginB</title></head><body>
<script>
var a = window.localStorage.getItem('secret_a') || '';
var b = window.localStorage.getItem('secret_b') || '';
document.title = 'ORIGB:' + b + '|LEAKA:' + a;
</script></body></html>"""
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv_a = http.server.ThreadingHTTPServer(("127.0.0.1", 0), HostileOriginAHandler)
    srv_b = http.server.ThreadingHTTPServer(("127.0.0.1", 0), BenignOriginBHandler)
    threading.Thread(target=srv_a.serve_forever, daemon=True).start()
    threading.Thread(target=srv_b.serve_forever, daemon=True).start()
    origin_a = f"http://127.0.0.1:{srv_a.server_address[1]}"
    origin_b = f"http://127.0.0.1:{srv_b.server_address[1]}"

    with tempfile.TemporaryDirectory(prefix="sf_sec_mcp_") as tmp:
        tmp_p = Path(tmp)
        out_dir = tmp_p / "out"
        out_dir.mkdir(mode=0o700)
        outside_file = tmp_p / "outside_secret.json"
        outside_file.write_text('{"cookies":[],"origins":[]}', encoding="utf-8")
        outside_missing = tmp_p / "does_not_exist_9999.json"

        client = MCPStdioClient(
            str(rust_mcp_bin),
            [f"--moli={moli_bin}", f"--output-dir={out_dir}", "--layout"],
            {},
        )
        try:
            client.initialize()

            # D6: tools/list exposes ZERO raw credential readers
            client._id += 1
            req_id = client._id
            client._send({"jsonrpc": "2.0", "id": req_id, "method": "tools/list", "params": {}})
            tools_resp = client._recv(req_id, timeout=15.0)
            tool_names = {t["name"] for t in tools_resp.get("result", {}).get("tools", [])}
            forbidden_tools = {
                "browser_cookie_list",
                "browser_cookie_get",
                "browser_storage_state",
                "browser_localstorage_get",
                "browser_localstorage_list",
                "browser_sessionstorage_get",
                "browser_sessionstorage_list",
            }
            leaked_tools = sorted(tool_names & forbidden_tools)
            runner.record(
                "D6_mcp_surface_and_redaction",
                "zero_raw_credential_reader_tools_exposed",
                len(leaked_tools) == 0 and len(tool_names) >= 15,
                f"{len(tool_names)} tools listed; forbidden credential-read tools found={leaked_tools}",
            )

            # D2: Navigation scheme sandbox
            bad_schemes = [
                ("file_scheme", "file:///etc/passwd"),
                ("javascript_scheme", "javascript:alert(1)"),
                ("data_html_scheme", "data:text/html,<h1>pwn</h1>"),
                ("chrome_internal", "chrome://settings"),
            ]
            blocked_schemes = 0
            missed_schemes: List[str] = []
            for label, bad_url in bad_schemes:
                r = client.call_tool("browser_navigate", {"url": bad_url, "snapshot": False})
                if r.get("result", {}).get("isError"):
                    blocked_schemes += 1
                else:
                    missed_schemes.append(label)
            runner.record(
                "D2_navigation_scheme_sandbox",
                "block_file_javascript_data_chrome_urls",
                blocked_schemes == len(bad_schemes),
                f"{blocked_schemes}/{len(bad_schemes)} unsafe URL schemes rejected fail-closed"
                + (f"; missed={missed_schemes}" if missed_schemes else ""),
            )

            # D3: Cross-origin isolation + hostile page JSON/prototype pollution resilience
            st1 = out_dir / "cross_v1.json"
            st1.write_text(
                json.dumps(
                    {
                        "cookies": [],
                        "origins": [
                            {"origin": origin_a, "localStorage": [{"name": "secret_a", "value": "A_TOKEN_V1"}]},
                            {"origin": origin_b, "localStorage": [{"name": "secret_b", "value": "B_TOKEN_V1"}]},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            os.chmod(st1, 0o600)
            client.call_tool("browser_set_storage_state", {"filename": str(st1)})

            nav_a = _extract_mcp_text(client.call_tool("browser_navigate", {"url": f"{origin_a}/", "snapshot": False}))
            ok_a1 = "ORIGA:A_TOKEN_V1|LEAKB:" in nav_a and "B_TOKEN_V1" not in nav_a

            # Hot-reload v2 WHILE already sitting on hostile Origin A (with hijacked JSON.stringify!)
            st2 = out_dir / "cross_v2.json"
            st2.write_text(
                json.dumps(
                    {
                        "cookies": [],
                        "origins": [
                            {"origin": origin_a, "localStorage": [{"name": "secret_a", "value": "A_TOKEN_V2"}]},
                            {"origin": origin_b, "localStorage": [{"name": "secret_b", "value": "B_TOKEN_V2"}]},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            os.chmod(st2, 0o600)
            client.call_tool("browser_set_storage_state", {"filename": str(st2)})
            ev_a2 = _extract_mcp_text(
                client.call_tool(
                    "browser_evaluate",
                    {"expression": "window.localStorage.getItem('secret_a') + '|' + (window.localStorage.getItem('secret_b') || 'NONE')"},
                )
            )
            ok_a2_hot = "A_TOKEN_V2|NONE" in ev_a2 and "B_TOKEN_V2" not in ev_a2 and "HIJACKED_JSON" not in ev_a2

            nav_b = _extract_mcp_text(client.call_tool("browser_navigate", {"url": f"{origin_b}/", "snapshot": False}))
            ok_b2 = "ORIGB:B_TOKEN_V2|LEAKA:" in nav_b and "A_TOKEN_V2" not in nav_b and "A_TOKEN_V1" not in nav_b

            runner.record(
                "D3_cross_origin_and_isolated_world",
                "origin_scoped_hydration_under_main_world_tampering",
                ok_a1 and ok_a2_hot and ok_b2,
                f"init_A_isolated={ok_a1}, hot_reload_A_immune_to_JSON_hijack={ok_a2_hot}, origin_B_isolated={ok_b2}",
            )

            # D4a: Screenshot path traversal, dotfile, non-png, symlink, and 0600 perms
            sym_shot = out_dir / "sym.png"
            sym_target = tmp_p / "sym_target.png"
            os.symlink(sym_target, sym_shot)

            bad_shots = [
                ("dotdot_traversal", "../../escape.png"),
                ("absolute_escape", str(tmp_p / "abs_escape.png")),
                ("hidden_dotfile", ".sessfork-state.json"),
                ("non_png_ext", "notes.txt"),
                ("symlink_target", "sym.png"),
            ]
            blocked_shots = 0
            missed_shots: List[str] = []
            for label, fname in bad_shots:
                r = client.call_tool("browser_take_screenshot", {"filename": fname})
                if r.get("result", {}).get("isError"):
                    blocked_shots += 1
                else:
                    missed_shots.append(label)
            ok_sym_untouched = not sym_target.exists()

            r_good_shot = client.call_tool("browser_take_screenshot", {"filename": "valid_audit.png"})
            shot_path = out_dir / "valid_audit.png"
            shot_mode = stat.S_IMODE(os.lstat(shot_path).st_mode) if shot_path.exists() else 0
            ok_good_shot = (not r_good_shot.get("result", {}).get("isError")) and shot_path.exists() and shot_mode == 0o600
            runner.record(
                "D4_filesystem_and_symlink_guards",
                "screenshot_confinement_symlink_block_and_0600_mode",
                blocked_shots == len(bad_shots) and ok_sym_untouched and ok_good_shot,
                f"blocked={blocked_shots}/{len(bad_shots)}, sym_untouched={ok_sym_untouched}, valid_png_mode={oct(shot_mode)}",
            )

            # D4b: browser_set_storage_state existence-oracle check
            r_exist = client.call_tool("browser_set_storage_state", {"filename": str(outside_file)})
            r_missing = client.call_tool("browser_set_storage_state", {"filename": str(outside_missing)})
            err_exist = _extract_mcp_text(r_exist)
            err_missing = _extract_mcp_text(r_missing)
            both_err = bool(r_exist.get("result", {}).get("isError")) and bool(r_missing.get("result", {}).get("isError"))
            identical_err = err_exist == err_missing and str(outside_file) not in err_exist
            runner.record(
                "D4_filesystem_and_symlink_guards",
                "storage_state_outside_root_no_existence_oracle",
                both_err and identical_err,
                f"both_blocked={both_err}, identical_sanitized_error={identical_err} ({err_exist[:72]!r})",
            )
        finally:
            client.close()
            srv_a.shutdown()
            srv_a.server_close()
            srv_b.shutdown()
            srv_b.server_close()


def verify_d4_python_state_guards(runner: SecurityAuditRunner) -> None:
    with tempfile.TemporaryDirectory(prefix="sf_sec_pystate_") as tmp:
        tmp_p = Path(tmp)
        real_target = tmp_p / "victim.json"
        real_target.write_text("DO_NOT_OVERWRITE", encoding="utf-8")
        sym_state = tmp_p / "state.json"
        os.symlink(real_target, sym_state)

        rejected_sym = False
        try:
            upsert_session_state([], [], state_path=sym_state)
        except BrowserProfileError:
            rejected_sym = True

        untouched = real_target.read_text(encoding="utf-8") == "DO_NOT_OVERWRITE"

        good_dir = tmp_p / "priv"
        good_state = good_dir / "state.json"
        upsert_session_state([], [], state_path=good_state)
        d_mode = stat.S_IMODE(os.lstat(good_dir).st_mode)
        f_mode = stat.S_IMODE(os.lstat(good_state).st_mode)
        runner.record(
            "D4_filesystem_and_symlink_guards",
            "python_state_refuses_symlink_and_enforces_0700_0600",
            rejected_sym and untouched and d_mode == 0o700 and f_mode == 0o600,
            f"symlink_rejected={rejected_sym}, victim_untouched={untouched}, dir={oct(d_mode)}, file={oct(f_mode)}",
        )


def verify_d5_native_host_security(runner: SecurityAuditRunner, rust_mcp_bin: Path) -> None:
    domain = "D5_native_host_scope_and_fail_closed"
    with tempfile.TemporaryDirectory(prefix="sf_sec_nh_") as tmp:
        home_p = Path(tmp)
        bad_domains = [
            "github.io",
            "co.uk",
            "com",
            "",
            "https://github.com@evil.com",
            "*.github.com",
            "javascript://github.com",
            "intranet",
        ]
        rejected = 0
        missed: List[str] = []
        for bad in bad_domains:
            _, resp, _ = _send_native_msg(
                rust_mcp_bin,
                {"action": "sync", "domain": bad, "cookies": [], "origins": []},
                home_p,
            )
            if resp.get("ok") is False:
                rejected += 1
            else:
                missed.append(bad)
        runner.record(
            domain,
            "reject_public_suffix_userinfo_and_path_spoof_domains",
            rejected == len(bad_domains),
            f"{rejected}/{len(bad_domains)} malicious domains rejected"
            + (f"; missed={missed}" if missed else ""),
        )

        # Cross-domain cookie/origin injection filter + zero secret echo
        secret_sentinel = "gho_SUPER_SECRET_SENTINEL_777888"
        _, resp_mix, raw_reply = _send_native_msg(
            rust_mcp_bin,
            {
                "action": "sync",
                "domain": "example.com",
                "cookies": [
                    {
                        "name": "session",
                        "value": secret_sentinel,
                        "domain": ".example.com",
                        "path": "/",
                        "secure": True,
                        "httpOnly": True,
                    },
                    {
                        "name": "injected",
                        "value": "EVIL_COOKIE_VALUE",
                        "domain": ".evil.com",
                        "path": "/",
                    },
                    {
                        "name": "slash_injected",
                        "value": "SLASH_COOKIE_VALUE",
                        "domain": "evil.com/.example.com",
                        "path": "/",
                    },
                ],
                "origins": [
                    {
                        "origin": "https://app.example.com",
                        "localStorage": [{"name": "jwt", "value": secret_sentinel}],
                    },
                    {
                        "origin": "https://evil.com",
                        "localStorage": [{"name": "hijack", "value": "EVIL_LS"}],
                    },
                ],
            },
            home_p,
        )
        state_file = home_p / ".sessfork" / "state.json"
        saved = json.loads(state_file.read_text(encoding="utf-8"))
        saved_domains = {c["domain"] for c in saved.get("cookies", [])}
        saved_origins = {o["origin"] for o in saved.get("origins", [])}
        st_mode = stat.S_IMODE(os.lstat(state_file).st_mode)
        no_secret_in_reply = secret_sentinel.encode("utf-8") not in raw_reply
        foreign_dropped = saved_domains == {".example.com"} and saved_origins == {"https://app.example.com"}
        legit_kept = (".example.com" in saved_domains) and ("https://app.example.com" in saved_origins)
        runner.record(
            domain,
            "drop_foreign_domain_cookies_and_origins_plus_zero_secret_echo",
            bool(resp_mix.get("ok")) and foreign_dropped and legit_kept and no_secret_in_reply and st_mode == 0o600,
            f"ok={resp_mix.get('ok')}, foreign_dropped={foreign_dropped}, legit_kept={legit_kept}, "
            f"secret_absent_from_reply={no_secret_in_reply}, file_mode={oct(st_mode)}",
        )

        # Corrupt state.json fail-closed (must refuse overwrite)
        state_file.write_text("{corrupt_truncated_json", encoding="utf-8")
        _, resp_corrupt, _ = _send_native_msg(
            rust_mcp_bin,
            {"action": "sync", "domain": "example.com", "cookies": [], "origins": []},
            home_p,
        )
        preserved = state_file.read_text(encoding="utf-8") == "{corrupt_truncated_json"
        runner.record(
            domain,
            "corrupt_state_json_fails_closed_without_wipe",
            resp_corrupt.get("ok") is False and preserved,
            f"rejected={resp_corrupt.get('ok') is False}, original_file_preserved={preserved}",
        )

        # Symlinked state.json refused
        victim = home_p / "victim_nh.json"
        victim.write_text("VICTIM_INTACT", encoding="utf-8")
        state_file.unlink()
        os.symlink(victim, state_file)
        _, resp_sym, _ = _send_native_msg(
            rust_mcp_bin,
            {"action": "sync", "domain": "example.com", "cookies": [], "origins": []},
            home_p,
        )
        victim_ok = victim.read_text(encoding="utf-8") == "VICTIM_INTACT"
        runner.record(
            domain,
            "native_host_refuses_symlink_state_file",
            resp_sym.get("ok") is False and victim_ok,
            f"rejected={resp_sym.get('ok') is False}, victim_intact={victim_ok}",
        )


def verify_d6_cli_redaction(runner: SecurityAuditRunner) -> None:
    with tempfile.TemporaryDirectory(prefix="sf_sec_cli_") as tmp:
        st_path = Path(tmp) / "state.json"
        sentinels = ["gho_CLI_SENTINEL_SECRET_111", "eyJhbGciOiJIUzI1NiJ9.CLI_JWT_SECRET.SIG"]
        upsert_session_state(
            [
                {
                    "name": "session",
                    "value": sentinels[0],
                    "domain": ".github.com",
                    "path": "/",
                    "expires": time.time() + 3600,
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                }
            ],
            [
                {
                    "origin": "https://github.com",
                    "localStorage": [{"name": "token", "value": sentinels[1]}],
                }
            ],
            state_path=st_path,
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT / "src")
        p = subprocess.run(
            [sys.executable, "-m", "sessfork", "--status", "--json", "--state", str(st_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            timeout=10,
            text=True,
        )
        leaked = [s for s in sentinels if s in p.stdout or s in p.stderr]
        parsed_ok = False
        try:
            obj = json.loads(p.stdout)
            parsed_ok = isinstance(obj, dict) and obj.get("total_cookies") == 1
        except Exception:
            parsed_ok = False
        runner.record(
            "D6_mcp_surface_and_redaction",
            "cli_status_json_strictly_redacts_all_secret_values",
            p.returncode == 0 and parsed_ok and len(leaked) == 0,
            f"rc={p.returncode}, valid_summary_json={parsed_ok}, leaked_sentinels={len(leaked)}",
        )


def verify_d7_studio_ui_guards(runner: SecurityAuditRunner) -> None:
    domain = "D7_studio_ui_auth_csrf_and_headers"
    with tempfile.TemporaryDirectory(prefix="sf_sec_ui_") as tmp:
        st_path = Path(tmp) / "state.json"
        upsert_session_state([], [], state_path=st_path)
        server, url, token = create_ui_server(state_path=st_path, target="auto", port=0)
        port = int(server.server_address[1])
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            def _req(method: str, path: str, headers: Dict[str, str], body: bytes = b"") -> Tuple[int, Dict[str, str]]:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                try:
                    conn.putrequest(method, path, skip_host=True)
                    for k, v in headers.items():
                        conn.putheader(k, v)
                    if body:
                        conn.putheader("Content-Length", str(len(body)))
                    conn.endheaders(body if body else None)
                    resp = conn.getresponse()
                    resp.read()
                    hdrs = {k.lower(): v for k, v in resp.getheaders()}
                    return resp.status, hdrs
                finally:
                    conn.close()

            host_ok = f"127.0.0.1:{port}"
            st_unauth_root, _ = _req("GET", "/", {"Host": host_ok})
            st_unauth_api, _ = _req("GET", "/api/status", {"Host": host_ok})
            st_bad_host, _ = _req("GET", "/api/status", {"Host": "evil.com", "X-Sessfork-Token": token})
            st_cross_site, _ = _req(
                "GET",
                "/api/status",
                {"Host": host_ok, "X-Sessfork-Token": token, "Sec-Fetch-Site": "cross-site"},
            )
            st_post_nocsrf, _ = _req(
                "POST",
                "/api/clear",
                {"Host": host_ok, "Content-Type": "application/json"},
                b'{"domain":"example.com"}',
            )
            st_post_bad_origin, _ = _req(
                "POST",
                "/api/clear",
                {
                    "Host": host_ok,
                    "Origin": "https://evil.com",
                    "X-Sessfork-Token": token,
                    "Content-Type": "application/json",
                },
                b'{"domain":"example.com"}',
            )
            st_auth_ok, ok_hdrs = _req(
                "GET",
                "/api/status",
                {"Host": host_ok, "X-Sessfork-Token": token},
            )

            all_403 = all(
                c == 403
                for c in (
                    st_unauth_root,
                    st_unauth_api,
                    st_bad_host,
                    st_cross_site,
                    st_post_nocsrf,
                    st_post_bad_origin,
                )
            )
            hdrs_ok = (
                st_auth_ok == 200
                and ok_hdrs.get("x-frame-options") == "DENY"
                and ok_hdrs.get("x-content-type-options") == "nosniff"
                and ok_hdrs.get("cache-control") == "no-store"
                and ok_hdrs.get("referrer-policy") == "no-referrer"
                and "frame-ancestors 'none'" in ok_hdrs.get("content-security-policy", "")
            )
            runner.record(
                domain,
                "enforce_session_auth_csrf_dns_rebinding_origin_and_csp",
                all_403 and hdrs_ok,
                f"6_attack_probes_all_403={all_403} "
                f"(codes={[st_unauth_root, st_unauth_api, st_bad_host, st_cross_site, st_post_nocsrf, st_post_bad_origin]}), "
                f"authed_200_with_strict_security_headers={hdrs_ok}",
            )
        finally:
            server.shutdown()
            server.server_close()


def verify_d8_python_public_suffix_guards(runner: SecurityAuditRunner) -> None:
    bad_inputs = ["", "   ", "com", "co.uk", "github.io", "appspot.com", "vercel.app", "amazonaws.com", "https://github.com@evil.com"]
    rejected = 0
    missed: List[str] = []
    for raw in bad_inputs:
        try:
            normalize_domain_filter(raw)
            missed.append(raw)
        except BrowserProfileError:
            rejected += 1
    runner.record(
        "D8_python_domain_allowlist",
        "reject_blank_tlds_cctlds_and_shared_cloud_public_suffixes",
        rejected == len(bad_inputs),
        f"{rejected}/{len(bad_inputs)} overly broad/malformed domain filters rejected"
        + (f"; missed={missed}" if missed else ""),
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run reproducible adversarial security verification for sessfork.")
    parser.add_argument("--json-out", type=Path, default=None, help="Optional JSON report path.")
    args = parser.parse_args(argv)

    rust_mcp_bin = find_rust_mcp_binary()
    moli_bin = find_moli_binary()
    if not rust_mcp_bin or not moli_bin:
        print(f"Missing required binaries: rust_mcp={rust_mcp_bin}, moli={moli_bin}", file=sys.stderr)
        return 2

    runner = SecurityAuditRunner()
    print("[security_verify] Executing live adversarial verification suite...", file=sys.stderr, flush=True)
    verify_d1_ssrf_cdp_allowlist(runner, rust_mcp_bin)
    verify_d2_d3_d4_d6_live_mcp(runner, rust_mcp_bin, moli_bin)
    verify_d4_python_state_guards(runner)
    verify_d5_native_host_security(runner, rust_mcp_bin)
    verify_d6_cli_redaction(runner)
    verify_d7_studio_ui_guards(runner)
    verify_d8_python_public_suffix_guards(runner)

    passed = sum(1 for c in runner.checks if c["status"] == "PASS")
    failed = sum(1 for c in runner.checks if c["status"] == "FAIL")
    report = {
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "rust_mcp_version": (get_rust_mcp_version(rust_mcp_bin) or "sessfork-mcp").strip(),
            "moli_version": (get_moli_version(moli_bin) or "moli").strip(),
        },
        "summary": {
            "total_checks": len(runner.checks),
            "passed": passed,
            "failed": failed,
            "all_passed": failed == 0,
        },
        "checks": runner.checks,
    }
    payload = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.json_out is not None:
        args.json_out.write_text(payload, encoding="utf-8")
    print(payload)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
