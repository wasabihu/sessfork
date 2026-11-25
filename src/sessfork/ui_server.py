"""Zero-dependency local control panel (`sessfork --ui`) strictly bound to 127.0.0.1.

Security guarantees:
- Binds exclusively to 127.0.0.1 (never 0.0.0.0).
- Requires a per-session 256-bit CSRF token (`X-Sessfork-Token`) and loopback Host/Origin on every POST.
- Strictly redacted: only calls `summarize_state()` and MCP inspection helpers; never reads or exposes
  raw Cookie values, LocalStorage token strings, or Keychain/AES secrets.
"""

from __future__ import annotations

import html
import http.server
import json
import os
import secrets
import socketserver
import sqlite3
import stat
import tempfile
import threading
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from sessfork import __version__
from sessfork.chrome import (
    BrowserProfileError,
    extract_browser_session,
    normalize_domain_filter,
    validate_profile_name,
)
from sessfork.crypto import BROWSER_KEYRING_MAP, CryptoError
from sessfork.localstorage import LevelDBFormatError
from sessfork.mcp_setup import (
    VALID_ENGINES,
    ensure_mcp_configured,
    inspect_mcp_headless_shell,
    install_agent_skills,
    target_mcp_config_paths,
)
from sessfork.moli_bridge import MOLI_INSTALL_CMD
from sessfork.state import (
    DEFAULT_STATE_PATH,
    clear_state,
    summarize_state,
    upsert_session_state,
)


def _client_label_from_path(cfg_path: str) -> str:
    low = cfg_path.lower()
    if ".claude.json" in low:
        return "Claude Code"
    if ".cursor" in low:
        return "Cursor"
    if "antigravity" in low or ".gemini" in low:
        return "Antigravity"
    return Path(cfg_path).name


def collect_ui_status(state_path: Path = DEFAULT_STATE_PATH, target: str = "auto") -> Dict[str, Any]:
    """Assemble redacted status JSON for the local control panel."""
    summary = summarize_state(state_path)
    config_paths = target_mcp_config_paths(target)
    shell_info = inspect_mcp_headless_shell(config_paths=config_paths)
    engine_modes: Dict[str, str] = shell_info.get("engine_modes") or {}

    # Derive current active engine mode across wired configs
    mode_values = set(engine_modes.values())
    if len(mode_values) == 1:
        active_engine = next(iter(mode_values))
    elif "dual" in mode_values:
        active_engine = "dual"
    elif "moli" in mode_values:
        active_engine = "moli"
    elif "cdp" in mode_values:
        active_engine = "cdp"
    else:
        active_engine = "chrome"

    clients: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for p in config_paths:
        ps = str(p)
        if ps in seen:
            continue
        seen.add(ps)
        wired = ps in engine_modes
        mode = engine_modes.get(ps, "unconfigured")
        servers = (
            ["sessfork-browser", "sessfork-moli"]
            if mode == "dual"
            else (["sessfork-browser"] if wired else [])
        )
        clients.append(
            {
                "name": _client_label_from_path(ps),
                "path": ps,
                "wired": wired,
                "mode": mode,
                "servers": servers,
                "cdp_endpoint": (shell_info.get("cdp_mcp_configs") or {}).get(ps),
            }
        )

    return {
        "version": __version__,
        "active_engine": active_engine,
        "chrome_shell_detected": bool(shell_info.get("playwright_headless_shell")),
        "chrome_shell_path": shell_info.get("playwright_headless_shell"),
        "moli_detected": bool(shell_info.get("moli_binary")),
        "moli_binary": shell_info.get("moli_binary"),
        "moli_version": shell_info.get("moli_version"),
        "rust_mcp_detected": bool(shell_info.get("rust_mcp_binary")),
        "rust_mcp_binary": shell_info.get("rust_mcp_binary"),
        "rust_mcp_version": shell_info.get("rust_mcp_version"),
        "moli_install_cmd": MOLI_INSTALL_CMD,
        "warning": shell_info.get("warning"),
        "clients": clients,
        "state": {
            "exists": summary["exists"],
            "path": summary["path"],
            "hot_reload_path": summary["hot_reload_path"],
            "total_cookies": summary["total_cookies"],
            "total_origins": summary["total_origins"],
            "total_local_storage_keys": summary["total_local_storage_keys"],
            "domains": summary["domains"],
            "origins": summary["origins"],
        },
    }


def render_ui_html(csrf_token: str) -> str:
    """Render the self-contained OLED dark control panel following ui-ux-pro-max rules."""
    return _UI_HTML_TEMPLATE.replace("__CSRF_TOKEN__", csrf_token).replace("__VERSION__", __version__)


class _PayloadTooLargeError(ValueError):
    """Raised when an HTTP JSON request body exceeds the allowed byte budget."""


class _ReusableLoopbackServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def _is_valid_host(host_header: str, bound_port: int) -> bool:
    """Strictly require exact loopback Host matching the bound port (blocks DNS rebinding)."""
    cleaned = host_header.strip().lower()
    if not cleaned or "://" in cleaned:
        return False
    return cleaned in (f"127.0.0.1:{bound_port}", f"localhost:{bound_port}")


def _is_valid_origin(origin_header: str, bound_port: int) -> bool:
    """Strictly require exact same-origin http loopback Origin matching the bound port."""
    cleaned = origin_header.strip().lower()
    return cleaned in (f"http://127.0.0.1:{bound_port}", f"http://localhost:{bound_port}")


def _is_allowed_fetch_site(header_val: str) -> bool:
    cleaned = header_val.strip().lower()
    return cleaned in ("", "same-origin", "none")


_UI_CSP = (
    "default-src 'none'; "
    "style-src 'unsafe-inline'; "
    "script-src 'unsafe-inline'; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'self'"
)


def create_ui_server(
    state_path: Path = DEFAULT_STATE_PATH,
    target: str = "auto",
    port: int = 0,
) -> tuple[_ReusableLoopbackServer, str, str]:
    """Create a loopback-only HTTP control server and return `(server, url, csrf_token)`."""
    csrf_token = secrets.token_urlsafe(32)
    session_cookie = secrets.token_urlsafe(32)
    html_bytes = render_ui_html(csrf_token).encode("utf-8")
    bound_port_box: List[int] = [0]

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_security_headers(self) -> None:
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", _UI_CSP)

        def _send_json(self, code: int, payload: Dict[str, Any]) -> None:
            body = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self._send_security_headers()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_session_bootstrap(self) -> None:
            # Return 200 + Set-Cookie + same-origin JS/meta redirect instead of HTTP 303 so
            # navigating from a local `file://` launcher commits an http://127.0.0.1 document
            # first; the subsequent navigation to `/` is strictly same-origin (`Sec-Fetch-Site:
            # same-origin` and `SameSite=Strict` cookie attached).
            body = (
                b'<!doctype html><html><head><meta charset="utf-8">'
                b'<meta http-equiv="refresh" content="0;url=/">'
                b'<title>Opening sessfork Studio...</title></head><body>'
                b'<script>window.location.replace("/");</script>'
                b'</body></html>\n'
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header(
                "Set-Cookie",
                f"sessfork_session={session_cookie}; HttpOnly; SameSite=Strict; Path=/",
            )
            self._send_security_headers()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _check_host_and_fetch_site(self, *, allow_cross_site_form: bool = False) -> bool:
            if not _is_valid_host(self.headers.get("Host", ""), bound_port_box[0]):
                return False
            if not allow_cross_site_form and not _is_allowed_fetch_site(self.headers.get("Sec-Fetch-Site", "")):
                return False
            return True

        def _has_valid_session_cookie(self) -> bool:
            raw_cookie = self.headers.get("Cookie", "")
            if not raw_cookie:
                return False
            # Parse semicolon-delimited pairs directly instead of SimpleCookie.load(), which
            # aborts on unrelated localhost dev cookies containing brackets or spaces.
            for part in raw_cookie.split(";"):
                name, sep, val = part.partition("=")
                if sep and name.strip() == "sessfork_session":
                    if secrets.compare_digest(val.strip().strip('"'), session_cookie):
                        return True
            return False

        def _has_valid_header_token(self) -> bool:
            supplied = self.headers.get("X-Sessfork-Token", "")
            return bool(supplied) and secrets.compare_digest(supplied, csrf_token)

        def _has_valid_read_auth(self) -> bool:
            return self._has_valid_header_token() or self._has_valid_session_cookie()

        def _check_csrf_and_origin(self) -> bool:
            if not self._check_host_and_fetch_site():
                return False
            origin = self.headers.get("Origin", "")
            if origin and not _is_valid_origin(origin, bound_port_box[0]):
                return False
            return self._has_valid_header_token()

        def _read_json_body(self) -> Dict[str, Any]:
            raw_len = self.headers.get("Content-Length", "0").strip()
            length = int(raw_len or "0")
            if length < 0:
                raise ValueError("Invalid Content-Length")
            if length > 65536:
                raise _PayloadTooLargeError("Request body exceeds 64 KiB limit")
            if length == 0:
                return {}
            ctype = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if ctype != "application/json":
                raise ValueError("Content-Type must be application/json")
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("JSON body must be an object")
            return data

        def do_GET(self) -> None:
            if not self._check_host_and_fetch_site():
                self._send_json(403, {"ok": False, "error": "Forbidden: invalid Host or cross-site request"})
                return
            parsed = urlparse(self.path)
            if parsed.path in ("/", "/index.html"):
                qs = parse_qs(parsed.query, keep_blank_values=False)
                q_tokens = qs.get("token")
                if q_tokens:
                    if secrets.compare_digest(q_tokens[0], csrf_token):
                        self._send_session_bootstrap()
                        return
                    self._send_json(403, {"ok": False, "error": "Forbidden: invalid session token"})
                    return
                if not self._has_valid_read_auth():
                    self._send_json(403, {"ok": False, "error": "Forbidden: authentication required"})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self._send_security_headers()
                self.send_header("Content-Length", str(len(html_bytes)))
                self.end_headers()
                self.wfile.write(html_bytes)
                return
            if parsed.path == "/api/status":
                if not self._has_valid_read_auth():
                    self._send_json(403, {"ok": False, "error": "Forbidden: authentication required"})
                    return
                self._send_json(200, {"ok": True, "data": collect_ui_status(state_path, target)})
                return
            self._send_json(404, {"ok": False, "error": "Not found"})

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/auth":
                # Local 0600 launcher HTML submits POST /auth with the one-session token so the
                # secret never appears in OS process arguments (`ps aux`).
                if not self._check_host_and_fetch_site(allow_cross_site_form=True):
                    self._send_json(403, {"ok": False, "error": "Forbidden: invalid Host"})
                    return
                origin = self.headers.get("Origin", "").strip().lower()
                if origin and origin != "null" and not _is_valid_origin(origin, bound_port_box[0]):
                    self._send_json(403, {"ok": False, "error": "Forbidden: invalid Origin"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0").strip() or "0")
                    if length <= 0 or length > 1024:
                        raise ValueError("Invalid auth payload size")
                    raw = self.rfile.read(length).decode("utf-8")
                    form = parse_qs(raw, keep_blank_values=False)
                    tok = (form.get("token") or [""])[0]
                except (ValueError, UnicodeDecodeError):
                    self._send_json(400, {"ok": False, "error": "Invalid auth form body"})
                    return
                if not tok or not secrets.compare_digest(tok, csrf_token):
                    self._send_json(403, {"ok": False, "error": "Forbidden: invalid session token"})
                    return
                self._send_session_bootstrap()
                return

            if not self._check_csrf_and_origin():
                self._send_json(403, {"ok": False, "error": "Forbidden: invalid CSRF token or origin"})
                return
            try:
                body = self._read_json_body()
            except _PayloadTooLargeError as exc:
                self._send_json(413, {"ok": False, "error": str(exc)})
                return
            except (ValueError, UnicodeDecodeError) as exc:
                self._send_json(400, {"ok": False, "error": f"Invalid JSON body: {exc}"})
                return

            try:
                if parsed.path == "/api/engine":
                    raw_engine = body.get("engine")
                    engine = raw_engine.strip().lower() if isinstance(raw_engine, str) else ""
                    if engine not in VALID_ENGINES:
                        self._send_json(400, {"ok": False, "error": f"Invalid engine: {engine!r}"})
                        return
                    updated = ensure_mcp_configured([target], state_path=state_path, engine=engine)
                    install_agent_skills()
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "updated_configs": [str(p) for p in updated],
                            "data": collect_ui_status(state_path, target),
                        },
                    )
                    return

                if parsed.path == "/api/clear":
                    clear_all = body.get("clear_all") is True
                    raw_domain = body.get("domain")
                    if clear_all:
                        domains: Optional[List[str]] = None
                    elif isinstance(raw_domain, str) and raw_domain.strip():
                        domains = [normalize_domain_filter(raw_domain)]
                    else:
                        self._send_json(
                            400,
                            {"ok": False, "error": "Specify a non-empty 'domain' or pass 'clear_all': true"},
                        )
                        return
                    rem_c, left_c, rem_o, left_o = clear_state(domains, state_path=state_path)
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "removed_cookies": rem_c,
                            "removed_origins": rem_o,
                            "remaining_cookies": left_c,
                            "remaining_origins": left_o,
                            "data": collect_ui_status(state_path, target),
                        },
                    )
                    return

                if parsed.path == "/api/fork":
                    raw_domain = body.get("domain")
                    if not isinstance(raw_domain, str) or not raw_domain.strip():
                        self._send_json(400, {"ok": False, "error": "Please enter a domain (e.g. github.com)"})
                        return
                    domain = normalize_domain_filter(raw_domain)
                    no_cookies = bool(body.get("no_cookies", False))
                    raw_profile = body.get("profile")
                    if raw_profile is None or (isinstance(raw_profile, str) and not raw_profile.strip()):
                        profile: Optional[str] = None
                    elif isinstance(raw_profile, str):
                        profile = validate_profile_name(raw_profile)
                    else:
                        self._send_json(400, {"ok": False, "error": "Invalid profile parameter"})
                        return
                    raw_browser = body.get("browser", "chrome")
                    if not isinstance(raw_browser, str):
                        self._send_json(400, {"ok": False, "error": "Invalid browser parameter"})
                        return
                    browser = raw_browser.strip().lower() or "chrome"
                    if browser not in BROWSER_KEYRING_MAP:
                        self._send_json(400, {"ok": False, "error": f"Unsupported browser: {browser!r}"})
                        return
                    cookies, origins, profile_label = extract_browser_session(
                        domains=[domain],
                        browser=browser,
                        profile=profile,
                        include_cookies=not no_cookies,
                        include_local_storage=True,
                    )
                    _, up_c, _, up_ls, _ = upsert_session_state(
                        new_cookies=None if no_cookies else cookies,
                        new_origins=origins,
                        refreshed_domains=[domain],
                        state_path=state_path,
                    )
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "domain": domain,
                            "profile": profile_label,
                            "forked_cookies": up_c,
                            "forked_ls_keys": up_ls,
                            "data": collect_ui_status(state_path, target),
                        },
                    )
                    return

                if parsed.path == "/api/shutdown":
                    self._send_json(200, {"ok": True, "message": "Shutting down local sessfork UI"})
                    threading.Thread(target=server.shutdown, daemon=True).start()
                    return

                self._send_json(404, {"ok": False, "error": "Not found"})
            except (BrowserProfileError, CryptoError, LevelDBFormatError, sqlite3.Error, OSError) as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})

    server = _ReusableLoopbackServer(("127.0.0.1", port), Handler)
    bound_port = int(server.server_address[1])
    bound_port_box[0] = bound_port
    url = f"http://127.0.0.1:{bound_port}/"
    return server, url, csrf_token


def _write_ui_launcher_html(parent_dir: Path, auth_url: str, token: str) -> Path:
    """Create a 0600 local HTML auto-POST launcher so the token never appears in OS argv."""
    parent_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    dir_st = os.lstat(parent_dir)
    if stat.S_ISLNK(dir_st.st_mode) or not stat.S_ISDIR(dir_st.st_mode):
        raise BrowserProfileError(f"Unsafe launcher directory: {parent_dir}")
    os.chmod(parent_dir, 0o700)
    fd, tmp_name = tempfile.mkstemp(prefix=".ui_launch_", suffix=".html", dir=str(parent_dir))
    fd_owned = True
    try:
        os.fchmod(fd, 0o600)
        fd_owned = False
        safe_url = html.escape(auth_url, quote=True)
        safe_tok = html.escape(token, quote=True)
        doc = (
            "<!doctype html><html><head><meta charset=\"utf-8\">"
            "<title>Opening sessfork Studio...</title></head><body>"
            f"<form id=\"f\" method=\"POST\" action=\"{safe_url}\">"
            f"<input type=\"hidden\" name=\"token\" value=\"{safe_tok}\">"
            "</form><script>document.getElementById('f').submit();</script>"
            "</body></html>\n"
        )
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(doc)
    except BaseException:
        if fd_owned:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return Path(tmp_name)


def serve_ui(
    state_path: Path = DEFAULT_STATE_PATH,
    target: str = "auto",
    port: int = 0,
    open_browser: bool = True,
) -> int:
    """Start the local 127.0.0.1 control panel and block until Ctrl+C or UI shutdown."""
    server, url, token = create_ui_server(state_path=state_path, target=target, port=port)
    auth_display_url = f"{url}?token={token}"
    print(
        f"sessfork Control Panel running at {auth_display_url} (127.0.0.1 only, press Ctrl+C to stop)",
        flush=True,
    )
    launcher: Optional[Path] = None
    if open_browser:
        try:
            launcher = _write_ui_launcher_html(state_path.expanduser().parent, url + "auth", token)
            open_target = launcher.as_uri()
        except (BrowserProfileError, OSError):
            open_target = auth_display_url
        threading.Timer(0.25, lambda: webbrowser.open(open_target)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if launcher is not None:
            try:
                launcher.unlink()
            except OSError:
                pass
    return 0


_UI_HTML_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>sessfork Studio — Browser Engine &amp; Session Control</title>
  <style>
    :root {
      --bg: #0B0F19;
      --bg-elevated: #111827;
      --card: #172138;
      --card-hover: #1D2944;
      --border: rgba(148, 163, 184, 0.16);
      --border-strong: rgba(148, 163, 184, 0.34);
      --fg: #F8FAFC;
      --fg-muted: #94A3B8;
      --fg-subtle: #64748B;
      --primary: #7C3AED;
      --primary-soft: rgba(124, 58, 237, 0.16);
      --primary-border: rgba(124, 58, 237, 0.45);
      --accent: #22C55E;
      --accent-soft: rgba(34, 197, 94, 0.14);
      --accent-border: rgba(34, 197, 94, 0.42);
      --sky: #38BDF8;
      --sky-soft: rgba(56, 189, 248, 0.14);
      --sky-border: rgba(56, 189, 248, 0.42);
      --danger: #EF4444;
      --danger-soft: rgba(239, 68, 68, 0.14);
      --danger-border: rgba(239, 68, 68, 0.42);
      --radius-lg: 18px;
      --radius-md: 12px;
      --radius-sm: 8px;
      --font-sans: -apple-system, BlinkMacSystemFont, "SF Pro Display", "Inter", "PingFang SC", "Segoe UI", sans-serif;
      --font-mono: "SF Mono", "JetBrains Mono", ui-monospace, Menlo, Consolas, monospace;
      --header-h: 64px;
    }
    *, *::before, *::after { box-sizing: border-box; }
    html {
      background: var(--bg);
      color: var(--fg);
      font-family: var(--font-sans);
      line-height: 1.5;
      scroll-padding-top: calc(var(--header-h) + 16px);
      -webkit-font-smoothing: antialiased;
    }
    body {
      margin: 0;
      min-height: 100vh;
      background:
        radial-gradient(880px 420px at 14% -4%, rgba(124, 58, 237, 0.14), transparent 60%),
        radial-gradient(840px 440px at 88% 0%, rgba(34, 197, 94, 0.10), transparent 60%),
        var(--bg);
    }
    button, input, select { font-family: inherit; }
    :focus-visible {
      outline: 2px solid var(--sky);
      outline-offset: 2px;
    }
    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after { transition-duration: 0.01ms !important; animation-duration: 0.01ms !important; }
    }
    .topbar {
      position: sticky;
      top: 0;
      z-index: 40;
      min-height: var(--header-h);
      background: rgba(11, 15, 25, 0.80);
      backdrop-filter: blur(18px) saturate(160%);
      -webkit-backdrop-filter: blur(18px) saturate(160%);
      border-bottom: 1px solid var(--border);
    }
    .topbar-inner {
      max-width: 1080px;
      margin: 0 auto;
      padding: 12px 24px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 10px;
      font-weight: 700;
      font-size: 16px;
      letter-spacing: -0.01em;
    }
    .brand-icon {
      width: 34px;
      height: 34px;
      border-radius: 10px;
      background: linear-gradient(135deg, rgba(124, 58, 237, 0.28), rgba(34, 197, 94, 0.22));
      border: 1px solid var(--border-strong);
      display: grid;
      place-items: center;
      color: var(--accent);
    }
    .ver-pill {
      font-family: var(--font-mono);
      font-size: 12px;
      padding: 2px 8px;
      border-radius: 999px;
      background: rgba(148, 163, 184, 0.12);
      color: var(--fg-muted);
      border: 1px solid var(--border);
    }
    .top-actions {
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .btn {
      min-height: 44px;
      padding: 0 14px;
      border-radius: var(--radius-sm);
      border: 1px solid var(--border-strong);
      background: rgba(23, 33, 56, 0.85);
      color: var(--fg);
      font-size: 13px;
      font-weight: 600;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      white-space: nowrap;
      flex-shrink: 0;
      cursor: pointer;
      transition: background 180ms ease, border-color 180ms ease, transform 150ms ease;
    }
    .btn:hover:not(:disabled) { background: var(--card-hover); border-color: rgba(248, 250, 252, 0.32); }
    .btn:disabled { opacity: 0.55; cursor: not-allowed; }
    .btn-primary {
      background: var(--accent);
      color: #06130C;
      border-color: var(--accent);
    }
    .btn-primary:hover:not(:disabled) { background: #16A34A; border-color: #16A34A; }
    .btn-danger {
      background: var(--danger-soft);
      color: #FCA5A5;
      border-color: var(--danger-border);
    }
    .btn-danger:hover:not(:disabled) { background: rgba(239, 68, 68, 0.24); }
    .btn-sm { min-height: 36px; padding: 0 11px; font-size: 12px; }
    .container {
      max-width: 1080px;
      margin: 0 auto;
      padding: 28px 24px 64px;
      display: grid;
      gap: 24px;
    }
    .section-card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: var(--radius-lg);
      padding: 22px 24px;
      box-shadow: 0 20px 44px -22px rgba(0, 0, 0, 0.65);
    }
    .section-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 18px;
      flex-wrap: wrap;
    }
    .section-head h2 {
      margin: 0;
      font-size: 18px;
      font-weight: 700;
      letter-spacing: -0.015em;
    }
    .section-sub {
      margin: 4px 0 0;
      font-size: 13.5px;
      color: var(--fg-muted);
    }
    .status-strip {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      padding: 6px 11px;
      border-radius: 999px;
      font-size: 12.5px;
      font-weight: 600;
      border: 1px solid var(--border);
      background: rgba(11, 15, 25, 0.6);
      color: var(--fg-muted);
    }
    .pill-ok { background: var(--accent-soft); border-color: var(--accent-border); color: #86EFAC; }
    .pill-sky { background: var(--sky-soft); border-color: var(--sky-border); color: #7DD3FC; }
    .pill-purple { background: var(--primary-soft); border-color: var(--primary-border); color: #D8B4FE; }
    .pill-warn { background: var(--danger-soft); border-color: var(--danger-border); color: #FCA5A5; }
    .dot { width: 8px; height: 8px; border-radius: 50%; background: currentColor; }
    .engine-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 14px;
    }
    @media (max-width: 860px) { .engine-grid { grid-template-columns: 1fr; } }
    .engine-card {
      position: relative;
      text-align: left;
      width: 100%;
      padding: 18px;
      border-radius: var(--radius-md);
      border: 1.5px solid var(--border);
      background: rgba(11, 15, 25, 0.56);
      color: var(--fg);
      cursor: pointer;
      display: flex;
      flex-direction: column;
      gap: 10px;
      transition: border-color 180ms ease, background 180ms ease, transform 150ms ease;
    }
    .engine-card:hover:not(:disabled) {
      background: rgba(17, 24, 39, 0.85);
      border-color: var(--border-strong);
      transform: translateY(-1px);
    }
    .engine-card[aria-checked="true"] {
      border-color: var(--accent);
      background: linear-gradient(180deg, rgba(34, 197, 94, 0.11), rgba(11, 15, 25, 0.72));
      box-shadow: 0 0 0 1px rgba(34, 197, 94, 0.25);
    }
    .engine-card:disabled { opacity: 0.52; cursor: not-allowed; }
    .engine-top {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      width: 100%;
    }
    .engine-title {
      font-size: 15px;
      font-weight: 700;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .tag {
      font-size: 11px;
      font-weight: 700;
      padding: 2px 8px;
      border-radius: 999px;
      letter-spacing: 0.02em;
    }
    .tag-rec { background: var(--accent-soft); color: #86EFAC; border: 1px solid var(--accent-border); }
    .tag-default { background: var(--sky-soft); color: #7DD3FC; border: 1px solid var(--sky-border); }
    .tag-fast { background: var(--primary-soft); color: #D8B4FE; border: 1px solid var(--primary-border); }
    .engine-desc {
      margin: 0;
      font-size: 13px;
      color: var(--fg-muted);
      line-height: 1.5;
    }
    .metrics {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
      margin-top: auto;
      padding-top: 8px;
      border-top: 1px solid var(--border);
    }
    .metric {
      background: rgba(23, 33, 56, 0.65);
      border-radius: 8px;
      padding: 7px 9px;
    }
    .metric-label { font-size: 11px; color: var(--fg-subtle); display: block; }
    .metric-val { font-family: var(--font-mono); font-size: 12.5px; font-weight: 700; color: var(--fg); }
    .engine-footer {
      margin-top: 16px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
      padding-top: 14px;
      border-top: 1px solid var(--border);
    }
    .hint { font-size: 13px; color: var(--fg-muted); }
    .hint code {
      font-family: var(--font-mono);
      font-size: 12px;
      background: rgba(11, 15, 25, 0.8);
      padding: 2px 6px;
      border-radius: 6px;
      border: 1px solid var(--border);
      color: #E2E8F0;
    }
    .clients-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 12px;
    }
    .client-box {
      padding: 14px 16px;
      border-radius: var(--radius-md);
      background: rgba(11, 15, 25, 0.56);
      border: 1px solid var(--border);
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    .client-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      font-weight: 700;
      font-size: 14px;
    }
    .client-path {
      font-family: var(--font-mono);
      font-size: 11.5px;
      color: var(--fg-subtle);
      word-break: break-all;
    }
    .fork-row {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      align-items: center;
      margin-bottom: 16px;
    }
    .text-input {
      flex: 1 1 240px;
      min-height: 44px;
      padding: 0 14px;
      border-radius: var(--radius-sm);
      border: 1px solid var(--border-strong);
      background: rgba(11, 15, 25, 0.82);
      color: var(--fg);
      font-size: 14px;
    }
    .check-label {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      min-height: 44px;
      padding: 0 10px;
      font-size: 13px;
      color: var(--fg-muted);
      cursor: pointer;
    }
    .domain-list {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
      gap: 10px;
    }
    .domain-item {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      padding: 11px 13px;
      border-radius: var(--radius-sm);
      background: rgba(11, 15, 25, 0.55);
      border: 1px solid var(--border);
    }
    .domain-item > div { min-width: 0; }
    .domain-name { font-family: var(--font-mono); font-size: 13px; font-weight: 600; word-break: break-all; }
    .domain-meta { font-size: 11.5px; color: var(--fg-muted); }
    .toast {
      position: fixed;
      right: 24px;
      bottom: 24px;
      z-index: 60;
      max-width: 420px;
      padding: 13px 16px;
      border-radius: var(--radius-md);
      border: 1px solid var(--accent-border);
      background: rgba(11, 15, 25, 0.96);
      color: var(--fg);
      font-size: 13.5px;
      box-shadow: 0 16px 36px rgba(0, 0, 0, 0.6);
      opacity: 0;
      transform: translateY(10px);
      pointer-events: none;
      transition: opacity 200ms ease, transform 200ms ease;
    }
    .toast.show { opacity: 1; transform: translateY(0); pointer-events: auto; }
    .toast.err { border-color: var(--danger-border); color: #FECACA; }
  </style>
</head>
<body>
  <header class="topbar">
    <div class="topbar-inner">
      <div class="brand">
        <span class="brand-icon" aria-hidden="true">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="6" r="3"/><circle cx="18" cy="6" r="3"/><circle cx="12" cy="18" r="3"/><path d="M6 9v2a4 4 0 0 0 4 4h4a4 4 0 0 0 4-4V9"/><path d="M12 15v-4"/></svg>
        </span>
        <span>sessfork Studio</span>
        <span class="ver-pill">v__VERSION__</span>
        <span class="pill pill-ok"><span class="dot" aria-hidden="true"></span>127.0.0.1 Local</span>
      </div>
      <div class="top-actions">
        <button type="button" class="btn btn-sm" id="lang-btn" aria-label="Toggle language">EN / 中文</button>
        <button type="button" class="btn btn-sm" id="refresh-btn">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><polyline points="21 3 21 9 15 9"/></svg>
          <span data-i18n="refresh">刷新状态</span>
        </button>
        <button type="button" class="btn btn-sm btn-danger" id="shutdown-btn">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M18.36 6.64a9 9 0 1 1-12.73 0"/><line x1="12" y1="2" x2="12" y2="12"/></svg>
          <span data-i18n="stop">关闭面板</span>
        </button>
      </div>
    </div>
  </header>

  <main class="container">
    <section class="section-card" aria-labelledby="engine-heading">
      <div class="section-head">
        <div>
          <h2 id="engine-heading" data-i18n="engineTitle">无头浏览器引擎模式</h2>
          <p class="section-sub" data-i18n="engineSub">选择单引擎或双通道按需兜底。Moli 模式下由 sessfork 自动托管启停，无需手动运行 moli serve。</p>
        </div>
        <div class="status-strip" id="binary-pills"></div>
      </div>

      <div class="engine-grid" role="radiogroup" aria-labelledby="engine-heading">
        <button type="button" class="engine-card" role="radio" id="card-chrome" data-engine="chrome" aria-checked="false">
          <div class="engine-top">
            <span class="engine-title">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#38BDF8" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="4"/><line x1="21.17" y1="8" x2="12" y2="8"/><line x1="3.95" y1="6.06" x2="8.54" y2="14"/><line x1="10.88" y1="21.94" x2="15.46" y2="14"/></svg>
              <span>Chrome Headless Shell</span>
            </span>
            <span class="tag tag-default" data-i18n="tagDefault">默认单通道</span>
          </div>
          <p class="engine-desc" data-i18n="chromeDesc">仅注册 sessfork-browser。最高网站兼容性，适合强反爬、复杂 SPA 与支付/后台管理页面。</p>
          <div class="metrics">
            <div class="metric"><span class="metric-label" data-i18n="mCompat">任务兼容率</span><span class="metric-val">99.9%</span></div>
            <div class="metric"><span class="metric-label" data-i18n="mRam">12 路重型总内存</span><span class="metric-val">2,531 MB (15 proc)</span></div>
          </div>
        </button>

        <button type="button" class="engine-card" role="radio" id="card-moli" data-engine="moli" aria-checked="false">
          <div class="engine-top">
            <span class="engine-title">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#C084FC" stroke-width="2" aria-hidden="true"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
              <span>Moli + Rust MCP</span>
            </span>
            <span class="tag tag-fast" data-i18n="tagFast">零 Node 极速</span>
          </div>
          <p class="engine-desc" data-i18n="moliDesc">纯 Rust sessfork-mcp 直连 Moli 管道：4.6ms 初始化、1.7 MB 空载、零 Node/TCP 端口，12 路省 2.27 GB。</p>
          <div class="metrics">
            <div class="metric"><span class="metric-label" data-i18n="mRam">12 路重型总内存</span><span class="metric-val">260 MB (-89.7%)</span></div>
            <div class="metric"><span class="metric-label" data-i18n="mSpeed">重型 / 轻量提速</span><span class="metric-val">1.94x / 14.8x</span></div>
          </div>
        </button>

        <button type="button" class="engine-card" role="radio" id="card-dual" data-engine="dual" aria-checked="false">
          <div class="engine-top">
            <span class="engine-title">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#22C55E" stroke-width="2" aria-hidden="true"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><polyline points="9 12 11 14 15 10"/></svg>
              <span>Dual 智能双通道</span>
            </span>
            <span class="tag tag-rec" data-i18n="tagRec">推荐</span>
          </div>
          <p class="engine-desc" data-i18n="dualDesc">平时走纯 Rust sessfork-moli（空载 1.7 MB / 零 Node），遇强反爬/WebGL 再惰性唤醒 Chrome（未调用前 0 MB）。</p>
          <div class="metrics">
            <div class="metric"><span class="metric-label" data-i18n="mIdle">平时常驻开销</span><span class="metric-val">Rust 1.7 MB</span></div>
            <div class="metric"><span class="metric-label" data-i18n="mFallback">兜底兼容率</span><span class="metric-val">99.9% (0 MB 惰性)</span></div>
          </div>
        </button>
      </div>

      <div class="engine-footer">
        <div class="hint" id="engine-hint"></div>
        <button type="button" class="btn btn-primary" id="apply-engine-btn">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><polyline points="20 6 9 17 4 12"/></svg>
          <span data-i18n="applyBtn">应用到所有已检测 AI 客户端</span>
        </button>
      </div>
    </section>

    <section class="section-card" aria-labelledby="clients-heading">
      <div class="section-head">
        <div>
          <h2 id="clients-heading" data-i18n="clientsTitle">AI 客户端 MCP 装配状态</h2>
          <p class="section-sub" data-i18n="clientsSub">自动发现本机 Antigravity、Claude Code 与 Cursor 配置；切换引擎后在客户端重载一次 MCP 即生效。</p>
        </div>
      </div>
      <div class="clients-grid" id="clients-grid"></div>
    </section>

    <section class="section-card" aria-labelledby="domains-heading">
      <div class="section-head">
        <div>
          <h2 id="domains-heading" data-i18n="domainsTitle">已分叉域名会话（全程脱敏）</h2>
          <p class="section-sub" id="state-summary"></p>
        </div>
      </div>

      <form class="fork-row" id="fork-form">
        <label for="domain-input" class="hint" style="position:absolute;width:1px;height:1px;overflow:hidden;">Domain</label>
        <input id="domain-input" class="text-input" type="text" placeholder="输入域名或 URL，如 github.com / linear.app" autocomplete="off" />
        <label class="check-label">
          <input type="checkbox" id="no-cookies-chk" />
          <span data-i18n="noCookies">仅 LocalStorage（免钥匙串弹窗）</span>
        </label>
        <button type="submit" class="btn btn-primary" id="fork-btn">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
          <span data-i18n="forkBtn">分叉域名</span>
        </button>
      </form>

      <div class="domain-list" id="domain-list"></div>
    </section>
  </main>

  <div class="toast" id="toast" role="status" aria-live="polite" aria-atomic="true"></div>

  <script>
    const CSRF_TOKEN = "__CSRF_TOKEN__";
    let lang = "zh";
    let latestData = null;
    let selectedEngine = "chrome";

    const I18N = {
      zh: {
        refresh: "刷新状态",
        stop: "关闭面板",
        engineTitle: "无头浏览器引擎模式",
        engineSub: "选择单引擎或双通道按需兜底。Moli 模式下由 sessfork 自动托管启停，无需手动运行 moli serve。",
        tagDefault: "默认单通道",
        tagFast: "零 Node 极速",
        tagRec: "推荐",
        chromeDesc: "仅注册 sessfork-browser。最高网站兼容性，适合强反爬、WebGL 与复杂支付/后台站点。",
        moliDesc: "纯 Rust sessfork-mcp 直连 Moli 管道：4.6ms 初始化、1.7 MB 空载、零 Node/TCP 端口，12 路省 2.27 GB。",
        dualDesc: "平时走纯 Rust sessfork-moli（空载 1.7 MB / 零 Node），遇强反爬/WebGL 再惰性唤醒 Chrome（未调用前 0 MB）。",
        mCompat: "任务兼容率",
        mRam: "12 路重型总内存",
        mSpeed: "重型 / 轻量提速",
        mIdle: "平时常驻开销",
        mFallback: "兜底兼容率",
        applyBtn: "应用到所有已检测 AI 客户端",
        clientsTitle: "AI 客户端 MCP 装配状态",
        clientsSub: "自动发现本机 Antigravity、Claude Code 与 Cursor 配置；切换引擎后在客户端重载一次 MCP 即生效。",
        domainsTitle: "已分叉域名会话（全程脱敏）",
        noCookies: "仅 LocalStorage（免钥匙串弹窗）",
        forkBtn: "分叉域名",
        clearBtn: "移除",
        appliedMsg: "配置已写入所有已检测 AI 客户端，请在编辑器中重载一次 MCP。",
        noDomains: "当前尚未分叉任何域名。在上方输入域名（如 github.com）即可按最小权限安全提取。",
        moliMissing: "未检测到 Moli 二进制，安装命令：",
        stoppedMsg: "本地配置面板服务已安全关闭，可关闭此标签页。"
      },
      en: {
        refresh: "Refresh",
        stop: "Shut Down",
        engineTitle: "Headless Browser Engine Mode",
        engineSub: "Choose a single backend or lazy dual-engine fallback. Managed Moli auto-spawns and cleans up on exit — never run `moli serve` manually.",
        tagDefault: "Default Single",
        tagFast: "Zero-Node Rust",
        tagRec: "Recommended",
        chromeDesc: "Wires only `sessfork-browser` (chrome-headless-shell). Maximum compatibility for WebGL & anti-bot portals.",
        moliDesc: "Pure Rust `sessfork-mcp` over Moli CDP pipe: 4.6 ms init, 1.7 MB idle, zero Node/TCP ports, saves 2.27 GB at 12x load.",
        dualDesc: "Default to pure Rust `sessfork-moli` (1.7 MB idle, zero Node) with lazy 0 MB Chrome fallback for anti-bot/WebGL.",
        mCompat: "Compatibility",
        mRam: "12x Heavy Total RAM",
        mSpeed: "Heavy / Light Speed",
        mIdle: "Idle Footprint",
        mFallback: "Fallback Rate",
        applyBtn: "Apply to Detected AI Clients",
        clientsTitle: "AI Client MCP Wiring",
        clientsSub: "Auto-discovers Antigravity, Claude Code, and Cursor. Reload MCP once in your editor after switching engines.",
        domainsTitle: "Forked Domain Sessions (Redacted)",
        noCookies: "LocalStorage only (zero Keychain prompt)",
        forkBtn: "Fork Domain",
        clearBtn: "Clear",
        appliedMsg: "Saved MCP config across detected AI clients. Reload MCP once in your editor to apply.",
        noDomains: "No forked domains yet. Enter a domain above (e.g. github.com) for least-privilege extraction.",
        moliMissing: "Moli binary not detected. Install via: ",
        stoppedMsg: "Local sessfork UI server stopped cleanly. You can close this tab."
      }
    };

    function t(key) { return (I18N[lang] && I18N[lang][key]) || key; }

    function showToast(msg, isErr = false) {
      const el = document.getElementById("toast");
      el.textContent = msg;
      el.className = "toast show" + (isErr ? " err" : "");
      clearTimeout(window.__toastTimer);
      window.__toastTimer = setTimeout(() => { el.classList.remove("show"); }, 4500);
    }

    function applyI18n() {
      document.querySelectorAll("[data-i18n]").forEach((el) => {
        el.textContent = t(el.getAttribute("data-i18n"));
      });
      document.getElementById("domain-input").placeholder =
        lang === "zh" ? "输入域名或 URL，如 github.com / linear.app" : "Enter domain or URL, e.g. github.com";
      if (latestData) renderData(latestData);
    }

    function selectEngineCard(mode) {
      selectedEngine = mode;
      ["chrome", "moli", "dual"].forEach((m) => {
        const card = document.getElementById("card-" + m);
        card.setAttribute("aria-checked", String(m === mode));
      });
      const hint = document.getElementById("engine-hint");
      const cliMap = {
        chrome: "sessfork --engine chrome",
        moli: "sessfork --engine moli",
        dual: "sessfork --engine dual"
      };
      hint.textContent = "";
      if (latestData && !latestData.moli_detected && (mode === "moli" || mode === "dual")) {
        hint.appendChild(document.createTextNode(t("moliMissing")));
        const codeEl = document.createElement("code");
        codeEl.textContent = latestData.moli_install_cmd;
        hint.appendChild(codeEl);
      } else {
        hint.appendChild(document.createTextNode(lang === "zh" ? "等效命令行：" : "Equivalent CLI: "));
        const codeEl = document.createElement("code");
        codeEl.textContent = cliMap[mode];
        hint.appendChild(codeEl);
      }
    }

    function renderData(data) {
      latestData = data;
      const pills = document.getElementById("binary-pills");
      pills.innerHTML = "";
      const cPill = document.createElement("span");
      cPill.className = "pill " + (data.chrome_shell_detected ? "pill-ok" : "pill-warn");
      cPill.innerHTML = '<span class="dot" aria-hidden="true"></span>' + (data.chrome_shell_detected ? "chrome-headless-shell" : "chrome-headless-shell missing");
      pills.appendChild(cPill);

      const mPill = document.createElement("span");
      mPill.className = "pill " + (data.moli_detected ? "pill-purple" : "pill-warn");
      const mDot = document.createElement("span");
      mDot.className = "dot";
      mDot.setAttribute("aria-hidden", "true");
      mPill.appendChild(mDot);
      mPill.appendChild(document.createTextNode(data.moli_detected ? (data.moli_version || "moli detected") : "moli not installed"));
      pills.appendChild(mPill);

      if (data.rust_mcp_detected) {
        const rPill = document.createElement("span");
        rPill.className = "pill pill-ok";
        const rDot = document.createElement("span");
        rDot.className = "dot";
        rDot.setAttribute("aria-hidden", "true");
        rPill.appendChild(rDot);
        rPill.appendChild(document.createTextNode((data.rust_mcp_version || "sessfork-mcp") + " · Rust Pipe"));
        pills.appendChild(rPill);
      }

      document.getElementById("card-moli").disabled = !data.moli_detected;
      document.getElementById("card-dual").disabled = !data.moli_detected;

      const cur = ["chrome", "moli", "dual"].includes(data.active_engine) ? data.active_engine : "chrome";
      selectEngineCard(cur);

      const cGrid = document.getElementById("clients-grid");
      cGrid.innerHTML = "";
      data.clients.forEach((c) => {
        const box = document.createElement("div");
        box.className = "client-box";
        const badgeCls = c.mode === "dual" ? "pill-ok" : (c.mode === "moli" ? "pill-purple" : (c.wired ? "pill-sky" : "pill-warn"));
        const head = document.createElement("div");
        head.className = "client-head";
        const nameSpan = document.createElement("span");
        nameSpan.textContent = c.name;
        const badgeSpan = document.createElement("span");
        badgeSpan.className = "pill " + badgeCls;
        badgeSpan.textContent = c.mode;
        head.appendChild(nameSpan);
        head.appendChild(badgeSpan);

        const srv = document.createElement("div");
        srv.className = "domain-meta";
        srv.textContent = c.servers.length ? c.servers.join(" + ") : "Not wired yet";

        const pathDiv = document.createElement("div");
        pathDiv.className = "client-path";
        pathDiv.textContent = c.path;

        box.appendChild(head);
        box.appendChild(srv);
        box.appendChild(pathDiv);
        cGrid.appendChild(box);
      });

      const st = data.state;
      document.getElementById("state-summary").textContent =
        (lang === "zh"
          ? `共 ${st.total_cookies} 条 Cookie、${st.total_local_storage_keys} 条 LocalStorage 键（覆盖 ${st.total_origins} 个 Origin）· 从不暴露明文凭证`
          : `${st.total_cookies} cookies, ${st.total_local_storage_keys} LocalStorage keys across ${st.total_origins} origin(s) — raw secrets are never exposed`);

      const dList = document.getElementById("domain-list");
      dList.innerHTML = "";
      const domainMap = {};
      Object.entries(st.domains || {}).forEach(([d, info]) => {
        domainMap[d] = { cookies: info.active || 0, httpOnly: info.http_only || 0, ls: 0 };
      });
      Object.entries(st.origins || {}).forEach(([orig, cnt]) => {
        let host = orig;
        try { host = new URL(orig).hostname; } catch {}
        if (!domainMap[host]) domainMap[host] = { cookies: 0, httpOnly: 0, ls: 0 };
        domainMap[host].ls += cnt;
      });
      const names = Object.keys(domainMap).sort();
      if (!names.length) {
        const empty = document.createElement("p");
        empty.className = "hint";
        empty.textContent = t("noDomains");
        dList.appendChild(empty);
        return;
      }
      names.forEach((dom) => {
        const info = domainMap[dom];
        const row = document.createElement("div");
        row.className = "domain-item";
        const left = document.createElement("div");
        const domEl = document.createElement("div");
        domEl.className = "domain-name";
        domEl.textContent = dom;
        const metaEl = document.createElement("div");
        metaEl.className = "domain-meta";
        metaEl.textContent = info.cookies + " cookies · " + info.ls + " LS keys";
        left.appendChild(domEl);
        left.appendChild(metaEl);
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "btn btn-sm btn-danger";
        btn.textContent = t("clearBtn");
        btn.addEventListener("click", () => clearDomain(dom));
        row.appendChild(left);
        row.appendChild(btn);
        dList.appendChild(row);
      });
    }

    async function api(path, payload = null) {
      const opts = payload === null
        ? { method: "GET", headers: { "X-Sessfork-Token": CSRF_TOKEN } }
        : {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-Sessfork-Token": CSRF_TOKEN },
            body: JSON.stringify(payload)
          };
      const resp = await fetch(path, opts);
      const data = await resp.json();
      if (!resp.ok || !data.ok) throw new Error(data.error || ("HTTP " + resp.status));
      return data;
    }

    async function loadStatus() {
      try {
        const res = await api("/api/status");
        renderData(res.data);
      } catch (err) {
        showToast(err.message, true);
      }
    }

    async function clearDomain(dom) {
      try {
        const res = await api("/api/clear", { domain: dom });
        renderData(res.data);
        showToast((lang === "zh" ? "已清理域名：" : "Cleared domain: ") + dom);
      } catch (err) {
        showToast(err.message, true);
      }
    }

    ["chrome", "moli", "dual"].forEach((m) => {
      document.getElementById("card-" + m).addEventListener("click", () => selectEngineCard(m));
    });

    document.getElementById("apply-engine-btn").addEventListener("click", async () => {
      const btn = document.getElementById("apply-engine-btn");
      btn.disabled = true;
      try {
        const res = await api("/api/engine", { engine: selectedEngine });
        renderData(res.data);
        showToast(t("appliedMsg"));
      } catch (err) {
        showToast(err.message, true);
      } finally {
        btn.disabled = false;
      }
    });

    document.getElementById("fork-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const input = document.getElementById("domain-input");
      const dom = input.value.trim();
      if (!dom) return;
      const btn = document.getElementById("fork-btn");
      btn.disabled = true;
      try {
        const res = await api("/api/fork", {
          domain: dom,
          no_cookies: document.getElementById("no-cookies-chk").checked
        });
        input.value = "";
        renderData(res.data);
        showToast(`Forked ${res.domain}: ${res.forked_cookies} cookies, ${res.forked_ls_keys} LS keys`);
      } catch (err) {
        showToast(err.message, true);
      } finally {
        btn.disabled = false;
      }
    });

    document.getElementById("refresh-btn").addEventListener("click", loadStatus);
    document.getElementById("lang-btn").addEventListener("click", () => {
      lang = lang === "zh" ? "en" : "zh";
      applyI18n();
    });
    document.getElementById("shutdown-btn").addEventListener("click", async () => {
      try {
        await api("/api/shutdown", {});
        showToast(t("stoppedMsg"));
      } catch (err) {
        showToast(err.message, true);
      }
    });

    applyI18n();
    loadStatus();
  </script>
</body>
</html>
"""
