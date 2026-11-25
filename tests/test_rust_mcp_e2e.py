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
import stat
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
        elif parsed.path == "/spa":
            html = """<!doctype html><html><head><title></title></head><body>
<div id="app" aria-label="Loading…"></div>
<script>
setTimeout(() => {
  document.title = 'HydratedSPA';
  document.getElementById('app').removeAttribute('aria-label');
  document.getElementById('app').innerHTML =
    '<h1>Dashboard Ready</h1>' +
    '<div id="div-btn" tabindex="0" aria-label="笔记管理" onclick="document.title=\\'DIV_CLICKED_OK\\'">笔记管理</div>' +
    '<a id="slow-link" href="/slow-dest">SlowNext</a>';
}, 320);
</script></body></html>"""
        elif parsed.path == "/slow-dest":
            time.sleep(0.45)
            html = """<!doctype html><html><head><title>SlowDestLoaded</title></head><body>
<h1>Slow Destination Content</h1><button id="done">Done</button></body></html>"""
        elif parsed.path == "/webmcp":
            html = """<!doctype html><html><head><title>WebMCPWorkspace</title></head><body>
<header><h1>Workspace</h1></header>
<main>
  <h2>Summary</h2>
  <p>Built with <strong>sessfork</strong> and <em>Moli</em>. See <a href="/docs">Docs</a>.</p>
  <div hidden>HIDDEN_TEXT_MUST_NOT_LEAK</div>
  <div aria-hidden="true">ARIA_HIDDEN_MUST_NOT_LEAK</div>
  <div style="display:none">STYLE_HIDDEN_MUST_NOT_LEAK</div>
  <blockquote>Structure first.</blockquote>
  <ul><li>Zero Node</li><li hidden>HIDDEN_LI_SECRET</li><li>Rust Pipe</li></ul>
  <table><thead><tr><th>Stack</th><th>RAM</th></tr></thead><tbody><tr><td>Moli</td><td>226 MiB</td></tr><tr hidden><td>HIDDEN_TR_SECRET</td><td>999</td></tr></tbody></table>
  <p>Inline code: <code>`backtick`</code></p>
  <pre><code class="language-rust">fn main() {}</code></pre>
  <form id="qform" toolname="quick_note" tooldescription="Save note immediately" toolautosubmit="true">
    <input name="text" value="" toolparamdescription="Note text" />
    <button type="submit">Save</button>
  </form>
  <form id="mform" toolname="approve_transfer" tooldescription="Requires confirmation">
    <input name="amount" value="" toolparamdescription="Amount" />
    <button type="submit">Approve</button>
  </form>
</main>
<script>
document.getElementById('qform').addEventListener('submit', (e) => {
  e.preventDefault();
  if (e.respondWith) e.respondWith(Promise.resolve({ saved: e.target.text.value, auto: true }));
});
document.getElementById('mform').addEventListener('submit', (e) => {
  e.preventDefault();
  if (e.respondWith) e.respondWith(Promise.resolve({ approved: e.target.amount.value, confirmed: true }));
});
const mc = document.modelContext || navigator.modelContext;
if (mc) {
  mc.registerTool({
    name: 'get_quota',
    description: 'Return authenticated quota from forked session',
    inputSchema: { type: 'object', properties: { project: { type: 'string' } }, required: ['project'] },
    execute: ({ project }) => ({
      project,
      hasCookie: document.cookie.includes('sid=webmcp_ck'),
      tier: localStorage.getItem('tier')
    })
  });
}
</script></body></html>"""
        elif parsed.path == "/evil_proto":
            html = """<!doctype html><html><head><title>UntrustedSite</title></head><body>
<h1>Untrusted Attacker Page</h1>
<script>
window.__stolen = null;
Object.defineProperty(Object.prototype, location.origin, {
  configurable: true,
  get() {
    try { window.__stolen = Object.keys(this); } catch (_) { window.__stolen = 'ERR'; }
    return undefined;
  }
});
JSON.stringify = () => '{"url":"https://github.com/settings/tokens","title":"Spoofed","readyState":"complete"}';
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


_LOCAL_RELEASE_BIN = Path(__file__).resolve().parents[1] / "rust" / "sessfork-mcp" / "target" / "release" / "sessfork-mcp"
RUST_MCP_BIN = _LOCAL_RELEASE_BIN if _LOCAL_RELEASE_BIN.is_file() else find_rust_mcp_binary()
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

                # Direct outside path blocked (and identical opaque error for nonexistent path)
                is_err, txt = client.call_tool("browser_set_storage_state", {"filename": str(outside)})
                self.assertTrue(is_err)
                self.assertIn("outside allowed roots", txt)
                is_err_missing, txt_missing = client.call_tool(
                    "browser_set_storage_state", {"filename": str(tmp_dir / "definitely_missing.json")}
                )
                self.assertTrue(is_err_missing)
                self.assertEqual(txt, txt_missing)

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

                # Dangerous URL schemes rejected (including about:* other than about:blank and data:)
                for bad_url in (
                    "file:///etc/passwd",
                    "javascript:alert(1)",
                    "about:settings",
                    "data:text/html,<h1>hi</h1>",
                ):
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

            # With --layout: fails closed on traversal/hidden/non-png/symlink; writes 0600 PNG on valid filename
            c_yes = _MCPClient(RUST_MCP_BIN, [f"--moli={MOLI_BIN}", f"--output-dir={out_dir}", "--layout"])
            try:
                c_yes.initialize()
                c_yes.call_tool("browser_navigate", {"url": f"{self.origin}/"})
                for bad_shot in ("../../safe_shot.png", ".sessfork-state.json", "evil.txt", "sub/shot.png"):
                    is_err, txt = c_yes.call_tool("browser_take_screenshot", {"filename": bad_shot})
                    self.assertTrue(is_err, f"Expected {bad_shot!r} to be rejected, got: {txt}")

                victim = Path(tmp) / "victim.png"
                victim.write_bytes(b"KEEP_VICTIM")
                os.symlink(victim, out_dir / "sym_shot.png")
                is_err, txt = c_yes.call_tool("browser_take_screenshot", {"filename": "sym_shot.png"})
                self.assertTrue(is_err, f"Expected symlink screenshot target to be rejected: {txt}")
                self.assertEqual(victim.read_bytes(), b"KEEP_VICTIM")

                is_err, txt = c_yes.call_tool("browser_take_screenshot", {"filename": "safe_shot.png"})
                self.assertFalse(is_err, txt)
                saved = out_dir / "safe_shot.png"
                self.assertTrue(saved.is_file(), f"PNG missing at {saved}: {txt}")
                self.assertTrue(saved.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
                self.assertEqual(stat.S_IMODE(os.stat(saved).st_mode), 0o600)
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

    def test_07_spa_hydration_wait_focusable_div_and_slow_click_nav(self) -> None:
        assert RUST_MCP_BIN is not None and MOLI_BIN is not None
        with tempfile.TemporaryDirectory(prefix="sf_e2e_spa_") as tmp:
            out_dir = Path(tmp) / "out"
            out_dir.mkdir(mode=0o700)
            client = _MCPClient(RUST_MCP_BIN, [f"--moli={MOLI_BIN}", f"--output-dir={out_dir}"])
            try:
                client.initialize()

                # 1. Delayed SPA hydration (320ms after loadEventFired) resolves on the VERY FIRST browser_navigate!
                is_err, spa_txt = client.call_tool("browser_navigate", {"url": f"{self.origin}/spa"})
                self.assertFalse(is_err, spa_txt)
                self.assertIn("HydratedSPA", spa_txt)
                self.assertIn("Dashboard Ready", spa_txt)

                # 2. Focusable non-semantic <div tabindex="0" aria-label="笔记管理"> receives a [ref=eN] and clicks cleanly
                m_div = re.search(r'"笔记管理" \[ref=(e\d+)\]', spa_txt)
                self.assertIsNotNone(m_div, f"Expected focusable div ref in:\n{spa_txt}")
                is_err, click_div = client.call_tool("browser_click", {"ref": m_div.group(1)})  # type: ignore[union-attr]
                self.assertFalse(is_err, click_div)
                self.assertIn("DIV_CLICKED_OK", click_div)

                # 3. Clicking a link to a slower 450ms server page waits for full load & returns populated snapshot!
                m_link = re.search(r'link "SlowNext" \[ref=(e\d+)\]', click_div)
                self.assertIsNotNone(m_link, f"Expected SlowNext link ref in:\n{click_div}")
                is_err, dest_txt = client.call_tool("browser_click", {"ref": m_link.group(1)})  # type: ignore[union-attr]
                self.assertFalse(is_err, dest_txt)
                self.assertIn("SlowDestLoaded", dest_txt)
                self.assertIn("Slow Destination Content", dest_txt)
            finally:
                client.close()

    def test_08_webmcp_authenticated_declarative_and_markdown_extraction(self) -> None:
        assert RUST_MCP_BIN is not None and MOLI_BIN is not None
        with tempfile.TemporaryDirectory(prefix="sf_e2e_webmcp_") as tmp:
            tmp_dir = Path(tmp)
            out_dir = tmp_dir / "out"
            out_dir.mkdir(mode=0o700)
            exp = time.time() + 7200
            state_file = tmp_dir / "state.json"
            state_file.write_text(
                json.dumps(
                    {
                        "cookies": [
                            {"name": "sid", "value": "webmcp_ck", "domain": "127.0.0.1", "path": "/", "expires": exp}
                        ],
                        "origins": [
                            {"origin": self.origin, "localStorage": [{"name": "tier", "value": "pro"}]}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            client = _MCPClient(
                RUST_MCP_BIN,
                [f"--moli={MOLI_BIN}", f"--storage-state={state_file}", f"--output-dir={out_dir}"],
            )
            try:
                client.initialize()
                is_err, nav = client.call_tool("browser_navigate", {"url": f"{self.origin}/webmcp", "snapshot": False})
                self.assertFalse(is_err, nav)

                # 1. Structure-first Markdown extraction (scoped selector + hidden-element filtering + fail-closed miss)
                is_err, md = client.call_tool("browser_extract_markdown", {"selector": "main"})
                self.assertFalse(is_err, md)
                self.assertIn("## Summary", md)
                self.assertIn("**sessfork**", md)
                self.assertIn("*Moli*", md)
                self.assertIn("[Docs](", md)
                self.assertIn("> Structure first.", md)
                self.assertIn("| Stack | RAM |", md)
                self.assertIn("```rust", md)
                self.assertNotIn("HIDDEN_TEXT_MUST_NOT_LEAK", md)
                self.assertNotIn("ARIA_HIDDEN_MUST_NOT_LEAK", md)
                self.assertNotIn("STYLE_HIDDEN_MUST_NOT_LEAK", md)
                self.assertNotIn("HIDDEN_LI_SECRET", md)
                self.assertNotIn("HIDDEN_TR_SECRET", md)
                self.assertIn("`` `backtick` ``", md)

                is_err, miss = client.call_tool("browser_extract_markdown", {"selector": "article.missing"})
                self.assertTrue(is_err)
                self.assertIn("article.missing", miss)

                # 2. WebMCP tool discovery (imperative + autosubmit form + manual-confirm form)
                is_err, cat = client.call_tool("browser_webmcp_list", {"waitMs": 1000})
                self.assertFalse(is_err, cat)
                self.assertIn("get_quota", cat)
                self.assertIn("quick_note", cat)
                self.assertIn("approve_transfer", cat)

                # 3. Authenticated imperative WebMCP call sees forked cookie & localStorage
                is_err, q_txt = client.call_tool(
                    "browser_webmcp_call", {"name": "get_quota", "arguments": {"project": "moli-bridge"}}
                )
                self.assertFalse(is_err, q_txt)
                q_obj = json.loads(q_txt)
                self.assertEqual(q_obj["output"]["project"], "moli-bridge")
                self.assertTrue(q_obj["output"]["hasCookie"])
                self.assertEqual(q_obj["output"]["tier"], "pro")

                # 4. Declarative autosubmit form works without confirm=true
                is_err, a_txt = client.call_tool(
                    "browser_webmcp_call", {"name": "quick_note", "arguments": {"text": "hello"}}
                )
                self.assertFalse(is_err, a_txt)
                self.assertEqual(json.loads(a_txt)["output"]["saved"], "hello")

                # 5. Declarative manual-confirm form fails closed unless confirm=true
                is_err, refuse = client.call_tool(
                    "browser_webmcp_call", {"name": "approve_transfer", "arguments": {"amount": "42"}}
                )
                self.assertTrue(is_err)
                self.assertIn("confirm=true", refuse)

                is_err, ok_txt = client.call_tool(
                    "browser_webmcp_call",
                    {"name": "approve_transfer", "arguments": {"amount": "42"}, "confirm": True},
                )
                self.assertFalse(is_err, ok_txt)
                self.assertEqual(json.loads(ok_txt)["output"]["approved"], "42")

                # 6. Navigating to a non-WebMCP page clears stale tool registrations
                is_err, _ = client.call_tool("browser_navigate", {"url": f"{self.origin}/", "snapshot": False})
                self.assertFalse(is_err)
                is_err, empty_cat = client.call_tool("browser_webmcp_list", {})
                self.assertFalse(is_err, empty_cat)
                self.assertIn("WebMCP Tools (0)", empty_cat)
            finally:
                client.close()

    def test_09_cross_origin_ls_isolation_and_authoritative_url(self) -> None:
        """Regression guard for SMCP-01 (P0 cross-origin LS leak) and SMCP-02 (P1 URL spoof)."""
        assert RUST_MCP_BIN is not None and MOLI_BIN is not None
        attacker_origin = f"http://localhost:{self.port}"
        with tempfile.TemporaryDirectory(prefix="sf_e2e_sop_") as tmp:
            out_dir = Path(tmp) / "out"
            out_dir.mkdir(mode=0o700)
            state_file = out_dir / "state.json"
            state_file.write_text(
                json.dumps(
                    {
                        "cookies": [],
                        "origins": [
                            {
                                "origin": self.origin,
                                "localStorage": [
                                    {"name": "jwt", "value": "TRUSTED_ONLY_JWT_SECRET_999"},
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            client = _MCPClient(RUST_MCP_BIN, [f"--moli={MOLI_BIN}", f"--output-dir={out_dir}"])
            try:
                client.initialize()
                # 1. Open untrusted attacker page (localhost != 127.0.0.1 origin) that poisons
                #    Object.prototype[location.origin] and window.JSON.stringify
                is_err, nav_txt = client.call_tool("browser_navigate", {"url": f"{attacker_origin}/evil_proto"})
                self.assertFalse(is_err, nav_txt)
                # Authoritative CDP URL must NOT be spoofed by poisoned window.JSON.stringify
                self.assertIn(f"- Page URL: {attacker_origin}/evil_proto", nav_txt)
                self.assertNotIn("https://github.com/settings/tokens", nav_txt)

                # Markdown extractor must also use authoritative CDP URL and ignore JSON.stringify hijack
                is_err, md_txt = client.call_tool("browser_extract_markdown", {})
                self.assertFalse(is_err, md_txt)
                self.assertIn(f"- Page URL: {attacker_origin}/evil_proto", md_txt)
                self.assertNotIn("https://github.com/settings/tokens", md_txt)
                self.assertIn("Untrusted Attacker Page", md_txt)

                # 2. Hot-reload storage state while attacker tab is open: attacker must steal NOTHING
                is_err, load_txt = client.call_tool("browser_set_storage_state", {"filename": str(state_file)})
                self.assertFalse(is_err, load_txt)

                is_err, ev_txt = client.call_tool(
                    "browser_evaluate",
                    {"function": "() => ({ stolen: window.__stolen, jwt: localStorage.getItem('jwt') })"},
                )
                self.assertFalse(is_err, ev_txt)
                parsed = json.loads(ev_txt)
                self.assertIsNone(parsed["stolen"])
                self.assertIsNone(parsed["jwt"])

                # 3. Navigating to the legitimate trusted origin still hydrates its LocalStorage cleanly
                is_err, who_txt = client.call_tool("browser_navigate", {"url": f"{self.origin}/whoami"})
                self.assertFalse(is_err, who_txt)
                self.assertIn("TRUSTED_ONLY_JWT_SECRET_999", _page_title(who_txt))
            finally:
                client.close()


if __name__ == "__main__":
    unittest.main()
