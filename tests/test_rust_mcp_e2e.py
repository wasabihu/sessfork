"""Comprehensive live E2E regression tests for the native Rust `sessfork-mcp` server.

Guards against all historical pitfalls across security, isolation, hot-reload staleness,
interactive DOM automation, layout screenshots, error recovery, and orphan process leaks.
Automatically skipped on machines without both `sessfork-mcp` and `moli` installed.
"""

from __future__ import annotations

import http.server
import json
import os
import re
import select
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from sessfork.moli_bridge import find_moli_binary, find_rust_mcp_binary


class _E2ETestHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        cookie_hdr = self.headers.get("Cookie", "")

        if parsed.path == "/whoami":
            html = f"""<!doctype html><html><head><title>Echo</title></head><body>
<pre id="out"></pre>
<script>
const ls = {{}};
for (let i = 0; i < localStorage.length; i++) {{
  const k = localStorage.key(i);
  ls[k] = localStorage.getItem(k);
}}
const data = {{ cookie: {json.dumps(cookie_hdr)}, ls }};
document.getElementById('out').textContent = JSON.stringify(data);
document.title = 'WHOAMI:' + JSON.stringify(data);
</script></body></html>"""
        elif parsed.path == "/form":
            html = """<!doctype html><html><head><title>FormPage</title></head><body>
<h1>Test Form</h1>
<form id="f" action="/result" method="get">
  <input id="uname" name="user" placeholder="Username" />
  <textarea id="bio" name="bio" placeholder="Bio"></textarea>
  <button id="submit-btn" type="submit">SubmitForm</button>
</form>
<button id="delayed-btn" type="button" onclick="setTimeout(() => { const d = document.createElement('div'); d.textContent = 'ASYNC_READY_OK'; document.body.appendChild(d); }, 120)">TriggerAsync</button>
<a id="next-link" href="/result?user=via_link">GoResult</a>
<script>
console.log("FORM_BOOT_LOG");
console.error("FORM_WARN_LOG");
localStorage.setItem("app_written", "keep_in_same_rev");
</script></body></html>"""
        elif parsed.path == "/result":
            qs = parse_qs(parsed.query)
            user = (qs.get("user") or [""])[0]
            bio = (qs.get("bio") or [""])[0]
            html = f"""<!doctype html><html><head><title>Result:{user}|{bio}</title></head><body>
<div id="res">{user}|{bio}</div>
<script>
document.title = 'Result:' + {json.dumps(user)} + '|' + {json.dumps(bio)} + '|app=' + (localStorage.getItem('app_written') || '');
</script></body></html>"""
        else:
            html = "<!doctype html><html><head><title>Home</title></head><body><p>Hello</p></body></html>"

        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _MCPClient:
    def __init__(self, binary: Path, args: List[str]):
        self.proc = subprocess.Popen(
            [str(binary)] + args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self._id = 0

    def request(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 20.0) -> Dict[str, Any]:
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self._id += 1
        req_id = self._id
        msg = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
        self.proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

        deadline = time.time() + timeout
        buf = b""
        while time.time() < deadline:
            ready, _, _ = select.select([self.proc.stdout], [], [], max(0.05, deadline - time.time()))
            if not ready:
                continue
            chunk = os.read(self.proc.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError("sessfork-mcp closed stdout unexpectedly")
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                s = line.decode("utf-8", errors="replace").strip()
                if not s or not s.startswith("{"):
                    continue
                parsed = json.loads(s)
                if parsed.get("id") == req_id:
                    return parsed
        raise TimeoutError(f"MCP request {method} timed out")

    def initialize(self) -> Dict[str, Any]:
        assert self.proc.stdin is not None
        r = self.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "e2e", "version": "1"}})
        self.proc.stdin.write((json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n").encode("utf-8"))
        self.proc.stdin.flush()
        return r

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> tuple[bool, str]:
        resp = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        res = resp.get("result", {})
        is_err = bool(res.get("isError", False))
        parts = [str(c.get("text", "")) for c in res.get("content", []) if isinstance(c, dict) and c.get("type") == "text"]
        return is_err, "\n".join(parts)

    def close(self) -> None:
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        if self.proc.poll() is None:
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=2)


def _child_pids(parent_pid: int) -> List[int]:
    out = subprocess.check_output(["ps", "-eo", "pid,ppid"], text=True)
    res: List[int] = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) == 2 and int(parts[1]) == parent_pid:
            res.append(int(parts[0]))
    return res


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _page_title(text: str) -> str:
    for ln in text.splitlines():
        if ln.strip().startswith("- Page Title:"):
            return ln.strip().split(":", 1)[1].strip()
    return ""


def _current_tab_index(text: str) -> int:
    for ln in text.splitlines():
        m = re.match(r"^\s*-\s*(\d+):\s*\(current\)", ln)
        if m:
            return int(m.group(1))
    raise AssertionError(f"Could not parse current tab index from:\n{text}")


RUST_MCP_BIN = find_rust_mcp_binary()
MOLI_BIN = find_moli_binary()


@unittest.skipUnless(
    RUST_MCP_BIN is not None and MOLI_BIN is not None,
    "Requires both native Rust `sessfork-mcp` and `moli` binaries installed",
)
class RustMcpE2ETestSuite(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _E2ETestHandler)
        cls.server.daemon_threads = True
        cls.port = cls.server.server_address[1]
        cls.origin = f"http://127.0.0.1:{cls.port}"
        cls.srv_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.srv_thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def test_01_security_redaction_symlink_and_scheme_guards(self) -> None:
        assert RUST_MCP_BIN is not None and MOLI_BIN is not None
        with tempfile.TemporaryDirectory(prefix="sf_e2e_sec_") as tmp:
            tmp_dir = Path(tmp)
            out_dir = tmp_dir / "allowed"
            out_dir.mkdir(mode=0o700)
            outside = tmp_dir / "outside_secret.json"
            outside.write_text('{"cookies":[],"origins":[]}', encoding="utf-8")
            symlink_escape = out_dir / "escape_link.json"
            symlink_escape.symlink_to(outside)

            valid_state = out_dir / "valid.json"
            valid_state.write_text(
                json.dumps(
                    {
                        "cookies": [
                            {
                                "name": "sid",
                                "value": "SUPER_SECRET_COOKIE_999",
                                "domain": "127.0.0.1",
                                "path": "/",
                                "httpOnly": False,
                                "secure": False,
                                "sameSite": "Lax",
                            }
                        ],
                        "origins": [
                            {
                                "origin": self.origin,
                                "localStorage": [{"name": "jwt", "value": "SUPER_SECRET_JWT_888"}],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            client = _MCPClient(RUST_MCP_BIN, [f"--moli={MOLI_BIN}", f"--output-dir={out_dir}"])
            try:
                client.initialize()
                tools_resp = client.request("tools/list")
                names = {t["name"] for t in tools_resp["result"]["tools"]}
                for forbidden in (
                    "browser_cookie_list",
                    "browser_cookie_get",
                    "browser_storage_state",
                    "browser_localstorage_get",
                ):
                    self.assertNotIn(forbidden, names)

                # Direct outside path blocked
                is_err, txt = client.call_tool("browser_set_storage_state", {"filename": str(outside)})
                self.assertTrue(is_err)
                self.assertIn("outside allowed roots", txt)

                # Path traversal blocked
                is_err, txt = client.call_tool(
                    "browser_set_storage_state", {"filename": str(out_dir / ".." / "outside_secret.json")}
                )
                self.assertTrue(is_err)
                self.assertIn("outside allowed roots", txt)

                # Symlink escaping allowed root blocked
                is_err, txt = client.call_tool("browser_set_storage_state", {"filename": str(symlink_escape)})
                self.assertTrue(is_err)
                self.assertIn("outside allowed roots", txt)

                # Valid state loads cleanly WITHOUT echoing any secret values
                is_err, txt = client.call_tool("browser_set_storage_state", {"filename": str(valid_state)})
                self.assertFalse(is_err, txt)
                self.assertNotIn("SUPER_SECRET_COOKIE_999", txt)
                self.assertNotIn("SUPER_SECRET_JWT_888", txt)

                # Dangerous URL schemes rejected
                for bad_url in ("file:///etc/passwd", "javascript:alert(1)"):
                    is_err, txt = client.call_tool("browser_navigate", {"url": bad_url})
                    self.assertTrue(is_err, f"Expected {bad_url} to be blocked")
                    self.assertIn("Blocked URL", txt)
            finally:
                client.close()

    def test_02_initial_state_hot_reload_stale_cleanup_and_50_origins(self) -> None:
        assert RUST_MCP_BIN is not None and MOLI_BIN is not None
        with tempfile.TemporaryDirectory(prefix="sf_e2e_reload_") as tmp:
            tmp_dir = Path(tmp)
            out_dir = tmp_dir / "playwright-output"
            out_dir.mkdir(mode=0o700)
            exp = time.time() + 7200

            init_file = tmp_dir / "state.json"
            init_file.write_text(
                json.dumps(
                    {
                        "cookies": [
                            {"name": "sid", "value": "init_v0", "domain": "127.0.0.1", "path": "/", "expires": exp},
                            {"name": "stale_ck", "value": "must_die", "domain": "127.0.0.1", "path": "/", "expires": exp},
                        ],
                        "origins": [
                            {
                                "origin": self.origin,
                                "localStorage": [
                                    {"name": "tok", "value": "ls_v0"},
                                    {"name": "stale_ls", "value": "must_vanish"},
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            client = _MCPClient(
                RUST_MCP_BIN,
                [f"--moli={MOLI_BIN}", f"--storage-state={init_file}", f"--output-dir={out_dir}"],
            )
            try:
                client.initialize()

                # 1. Cold start auto-loads --storage-state
                is_err, nav0 = client.call_tool("browser_navigate", {"url": f"{self.origin}/whoami?s=0"})
                self.assertFalse(is_err, nav0)
                t0 = _page_title(nav0)
                self.assertIn("sid=init_v0", t0)
                self.assertIn("stale_ck=must_die", t0)
                self.assertIn('"tok":"ls_v0"', t0)
                self.assertIn('"stale_ls":"must_vanish"', t0)

                # 2. Hot-reload v1 removes BOTH stale_ck AND stale_ls, updates sid & tok (plus 49 extra origins!)
                cookies_v1 = [
                    {"name": "sid", "value": "fresh_v1", "domain": "127.0.0.1", "path": "/", "expires": exp}
                ]
                origins_v1 = [
                    {"origin": self.origin, "localStorage": [{"name": "tok", "value": "ls_v1"}]}
                ]
                for i in range(49):
                    dom = f"multi-{i:02d}.test"
                    cookies_v1.append(
                        {"name": "c0", "value": f"v{i}", "domain": "." + dom, "path": "/", "expires": exp, "secure": True}
                    )
                    origins_v1.append({"origin": f"https://{dom}", "localStorage": [{"name": "k", "value": f"v{i}"}]})

                v1_file = out_dir / "v1.json"
                v1_file.write_text(json.dumps({"cookies": cookies_v1, "origins": origins_v1}), encoding="utf-8")

                is_err, r_txt = client.call_tool("browser_set_storage_state", {"filename": str(v1_file)})
                self.assertFalse(is_err, r_txt)
                self.assertIn("50 cookie(s), 50 origin(s)", r_txt)

                is_err, nav1 = client.call_tool("browser_navigate", {"url": f"{self.origin}/whoami?s=1"})
                self.assertFalse(is_err, nav1)
                t1 = _page_title(nav1)
                self.assertIn("sid=fresh_v1", t1)
                self.assertIn('"tok":"ls_v1"', t1)
                # Stale credentials MUST NOT survive hot-reload!
                self.assertNotIn("stale_ck", t1)
                self.assertNotIn("must_die", t1)
                self.assertNotIn("stale_ls", t1)
                self.assertNotIn("must_vanish", t1)
            finally:
                client.close()

    def test_03_multi_context_isolation_and_tab_lifecycle(self) -> None:
        assert RUST_MCP_BIN is not None and MOLI_BIN is not None
        with tempfile.TemporaryDirectory(prefix="sf_e2e_iso_") as tmp:
            out_dir = Path(tmp) / "out"
            out_dir.mkdir(mode=0o700)
            exp = time.time() + 7200
            client = _MCPClient(RUST_MCP_BIN, [f"--moli={MOLI_BIN}", f"--output-dir={out_dir}"])
            try:
                client.initialize()
                tab_map: Dict[int, int] = {}
                for idx in range(4):
                    sf = out_dir / f"ctx-{idx}.json"
                    sf.write_text(
                        json.dumps(
                            {
                                "cookies": [
                                    {"name": "sid", "value": f"iso_ck_{idx}", "domain": "127.0.0.1", "path": "/", "expires": exp}
                                ],
                                "origins": [
                                    {"origin": self.origin, "localStorage": [{"name": "tok", "value": f"iso_ls_{idx}"}]}
                                ],
                            }
                        ),
                        encoding="utf-8",
                    )
                    is_err, t_txt = client.call_tool("browser_tabs", {"action": "new", "isolatedContext": f"iso-{idx}"})
                    self.assertFalse(is_err, t_txt)
                    tab_map[idx] = _current_tab_index(t_txt)
                    is_err, _ = client.call_tool("browser_set_storage_state", {"filename": str(sf)})
                    self.assertFalse(is_err)
                    is_err, nav = client.call_tool("browser_navigate", {"url": f"{self.origin}/whoami?init={idx}"})
                    self.assertFalse(is_err, nav)

                # Interleaved verification: 0 leaks across all 4 contexts
                for idx in (3, 0, 2, 1):
                    is_err, _ = client.call_tool("browser_tabs", {"action": "select", "index": tab_map[idx]})
                    self.assertFalse(is_err)
                    is_err, nav = client.call_tool("browser_navigate", {"url": f"{self.origin}/whoami?chk={idx}"})
                    self.assertFalse(is_err, nav)
                    tl = _page_title(nav)
                    self.assertIn(f"sid=iso_ck_{idx}", tl)
                    self.assertIn(f'"tok":"iso_ls_{idx}"', tl)
                    for other in range(4):
                        if other != idx:
                            self.assertNotIn(f"iso_ck_{other}", tl)
                            self.assertNotIn(f"iso_ls_{other}", tl)

                # Close middle tab & verify remaining tabs stay healthy
                is_err, _ = client.call_tool("browser_tabs", {"action": "close", "index": 2})
                self.assertFalse(is_err)
                is_err, list_txt = client.call_tool("browser_tabs", {"action": "list"})
                self.assertFalse(is_err)
                self.assertIn("(current)", list_txt)
            finally:
                client.close()

    def test_04_interactive_dom_forms_back_wait_console_and_error_recovery(self) -> None:
        assert RUST_MCP_BIN is not None and MOLI_BIN is not None
        with tempfile.TemporaryDirectory(prefix="sf_e2e_dom_") as tmp:
            out_dir = Path(tmp) / "out"
            out_dir.mkdir(mode=0o700)
            client = _MCPClient(RUST_MCP_BIN, [f"--moli={MOLI_BIN}", f"--output-dir={out_dir}"])
            try:
                client.initialize()
                is_err, nav = client.call_tool("browser_navigate", {"url": f"{self.origin}/form"})
                self.assertFalse(is_err, nav)

                m_user = re.search(r'textbox "Username" \[ref=(e\d+)\]', nav)
                m_bio = re.search(r'textbox "Bio" \[ref=(e\d+)\]', nav)
                m_delay = re.search(r'button "TriggerAsync" \[ref=(e\d+)\]', nav)
                m_sub = re.search(r'button "SubmitForm" \[ref=(e\d+)\]', nav)
                self.assertTrue(m_user and m_bio and m_delay and m_sub, f"Missing refs in:\n{nav}")

                # Console messages captured
                is_err, logs = client.call_tool("browser_console_messages")
                self.assertFalse(is_err)
                self.assertIn("FORM_BOOT_LOG", logs)
                self.assertIn("FORM_WARN_LOG", logs)

                # Type with clear=True, then append with clear=False on textarea
                is_err, _ = client.call_tool("browser_type", {"ref": m_user.group(1), "text": "alice", "clear": True})
                self.assertFalse(is_err)
                is_err, _ = client.call_tool("browser_type", {"ref": m_bio.group(1), "text": "hello", "clear": True})
                self.assertFalse(is_err)
                is_err, _ = client.call_tool("browser_type", {"ref": m_bio.group(1), "text": "_world", "clear": False})
                self.assertFalse(is_err)

                # Async wait_for dynamic text after clicking button
                is_err, _ = client.call_tool("browser_click", {"ref": m_delay.group(1)})
                self.assertFalse(is_err)
                is_err, w_txt = client.call_tool("browser_wait_for", {"text": "ASYNC_READY_OK"})
                self.assertFalse(is_err, w_txt)

                # Evaluate both arrow function, async promise, and expression
                is_err, ev1 = client.call_tool("browser_evaluate", {"function": "() => 6 * 7"})
                self.assertFalse(is_err, ev1)
                self.assertEqual(ev1.strip(), "42")
                is_err, ev2 = client.call_tool("browser_evaluate", {"function": "async () => await Promise.resolve('async_ok')"})
                self.assertFalse(is_err, ev2)
                self.assertIn("async_ok", ev2)

                # Error recovery: invalid ref & JS throw must NOT kill the server
                is_err, bad_ref = client.call_tool("browser_click", {"ref": "e99999"})
                self.assertTrue(is_err)
                self.assertIn("not found", bad_ref)
                is_err, bad_js = client.call_tool("browser_evaluate", {"function": "() => { throw new Error('boom'); }"})
                self.assertTrue(is_err)

                # Submit form via CSS selector click -> navigates to /result and preserves same-rev app_written LS
                is_err, res_nav = client.call_tool("browser_click", {"selector": "#submit-btn"})
                self.assertFalse(is_err, res_nav)
                # Give form navigation a moment if triggered asynchronously
                if "/result" not in res_nav:
                    is_err, res_nav = client.call_tool("browser_snapshot")
                self.assertIn("Result:alice|hello_world|app=keep_in_same_rev", res_nav)

                # Navigate back returns to /form
                is_err, back_nav = client.call_tool("browser_navigate_back")
                self.assertFalse(is_err, back_nav)
                self.assertIn("/form", back_nav)
            finally:
                client.close()

    def test_05_screenshot_guard_and_layout_png_output(self) -> None:
        assert RUST_MCP_BIN is not None and MOLI_BIN is not None
        with tempfile.TemporaryDirectory(prefix="sf_e2e_shot_") as tmp:
            out_dir = Path(tmp) / "shots"
            out_dir.mkdir(mode=0o700)

            # Without --layout: returns clear error
            c_no = _MCPClient(RUST_MCP_BIN, [f"--moli={MOLI_BIN}", f"--output-dir={out_dir}"])
            try:
                c_no.initialize()
                c_no.call_tool("browser_navigate", {"url": f"{self.origin}/"})
                is_err, txt = c_no.call_tool("browser_take_screenshot", {"filename": "a.png"})
                self.assertTrue(is_err)
                self.assertIn("--layout", txt)
            finally:
                c_no.close()

            # With --layout: writes valid PNG strictly inside out_dir (strips ../../ traversal!)
            c_yes = _MCPClient(RUST_MCP_BIN, [f"--moli={MOLI_BIN}", f"--output-dir={out_dir}", "--layout"])
            try:
                c_yes.initialize()
                c_yes.call_tool("browser_navigate", {"url": f"{self.origin}/"})
                is_err, txt = c_yes.call_tool("browser_take_screenshot", {"filename": "../../safe_shot.png"})
                self.assertFalse(is_err, txt)
                saved = out_dir / "safe_shot.png"
                self.assertTrue(saved.is_file(), f"PNG missing at {saved}: {txt}")
                self.assertTrue(saved.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
                self.assertFalse((Path(tmp) / "safe_shot.png").exists())
            finally:
                c_yes.close()

    def test_06_close_reopen_and_sigkill_zero_orphan_moli(self) -> None:
        assert RUST_MCP_BIN is not None and MOLI_BIN is not None
        with tempfile.TemporaryDirectory(prefix="sf_e2e_life_") as tmp:
            out_dir = Path(tmp) / "out"
            out_dir.mkdir(mode=0o700)
            client = _MCPClient(RUST_MCP_BIN, [f"--moli={MOLI_BIN}", f"--output-dir={out_dir}"])
            try:
                client.initialize()
                # Lazy startup: 0 child moli before first tool call
                self.assertEqual(_child_pids(client.proc.pid), [])

                is_err, _ = client.call_tool("browser_navigate", {"url": f"{self.origin}/"})
                self.assertFalse(is_err)
                kids1 = _child_pids(client.proc.pid)
                self.assertEqual(len(kids1), 1)
                first_moli_pid = kids1[0]

                # Explicit browser_close terminates child moli cleanly
                is_err, _ = client.call_tool("browser_close")
                self.assertFalse(is_err)
                time.sleep(0.1)
                self.assertFalse(_pid_alive(first_moli_pid))

                # Subsequent navigate lazily re-spawns a fresh moli cleanly!
                is_err, nav2 = client.call_tool("browser_navigate", {"url": f"{self.origin}/"})
                self.assertFalse(is_err, nav2)
                kids2 = _child_pids(client.proc.pid)
                self.assertEqual(len(kids2), 1)
                second_moli_pid = kids2[0]

                # Even abrupt SIGKILL on sessfork-mcp closes FD 3 -> moli self-terminates (0 orphans!)
                os.kill(client.proc.pid, signal.SIGKILL)
                client.proc.wait(timeout=5)
                deadline = time.time() + 3.0
                while time.time() < deadline and _pid_alive(second_moli_pid):
                    time.sleep(0.05)
                self.assertFalse(_pid_alive(second_moli_pid), f"Orphan moli PID {second_moli_pid} survived SIGKILL!")
            finally:
                client.close()


if __name__ == "__main__":
    unittest.main()
