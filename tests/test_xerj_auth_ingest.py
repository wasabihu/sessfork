"""TDD tests for skills/xerj-auth-ingest/ingest_to_xerj.py.

Failure modes guarded:
- Raw HTML scripts/styles/SVGs/comments/boilerplate leaking into indexed Markdown or corrupting CJK text
- Accidental credentials (Authorization Bearer/ApiKey, Cookie/Set-Cookie, hf_, ghp_, sk-, JWT) surviving into output Markdown
- SSRF / unintended exfiltration to non-loopback XERJ URLs or plaintext remote HTTP endpoints
- Path traversal ('..'), symlink escapes, or credential-file ingestion (e.g. ~/.sessfork/state.json, .env)
- Unsafe shell execution or accidental indexing during default --dry-run mode
- End-to-end indexing/search regression against an isolated temporary XERJ node (never touching :9200)
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "skills" / "xerj-auth-ingest" / "ingest_to_xerj.py"
SKILL_MD_PATH = REPO_ROOT / "skills" / "xerj-auth-ingest" / "SKILL.md"


def _load_ingest_module():
    if not SCRIPT_PATH.exists():
        raise FileNotFoundError(f"Missing ingest script: {SCRIPT_PATH}")
    spec = importlib.util.spec_from_file_location("xerj_auth_ingest", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["xerj_auth_ingest"] = module
    spec.loader.exec_module(module)
    return module


def _find_xerj_binary() -> str | None:
    preferred = Path.home() / ".local" / "bin" / "xerj"
    if preferred.is_file() and os.access(preferred, os.X_OK):
        return str(preferred)
    return shutil.which("xerj")


def _find_three_consecutive_free_ports() -> int:
    """XERJ --port P claims P (ES API), P+1 (REST), and P+2 (gRPC)."""
    for candidate in range(19400, 29400, 7):
        sockets: list[socket.socket] = []
        try:
            for offset in range(3):
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("127.0.0.1", candidate + offset))
                sockets.append(sock)
            return candidate
        except OSError:
            continue
        finally:
            for sock in sockets:
                sock.close()
    raise RuntimeError("Could not find 3 consecutive free loopback ports")


def _loopback_http_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, object] | None = None,
    timeout: float = 5.0,
) -> tuple[int, dict[str, object]]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with opener.open(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


class XerjAuthIngestTestSuite(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load_ingest_module()

    def test_skill_markdown_contains_required_workflow_and_guardrails(self) -> None:
        self.assertTrue(SKILL_MD_PATH.is_file(), f"Missing {SKILL_MD_PATH}")
        text = SKILL_MD_PATH.read_text(encoding="utf-8")
        self.assertIn("name: xerj-auth-ingest", text)
        self.assertIn("ingest_to_xerj.py", text)
        self.assertIn("xerj", text)
        self.assertIn("127.0.0.1", text)
        self.assertIn("<REDACTED>", text)
        self.assertIn("2FA", text)

    def test_html_stripping_frontmatter_and_cjk_preservation(self) -> None:
        raw_html = """<!doctype html>
        <html lang="zh-CN">
        <head>
          <meta charset="utf-8">
          <title>Q4 财务合规复盘 — Notion</title>
          <link rel="canonical" href="https://www.notion.so/team/q4-compliance-review">
          <style>.secret-css { display: none; }</style>
          <script>window.__BOOTSTRAP__ = "do_not_index_js";</script>
        </head>
        <body>
          <!-- internal HTML comment should vanish -->
          <nav><ul><li>Workspace Home</li><li>Logout</li></ul></nav>
          <main>
            <h1>Q4 财务合规复盘</h1>
            <p>依据《民法典》第3条与 IFRS 15 收入确认口径，核对繁體中文與简体中文条目。</p>
            <svg width="100" height="100"><text>SVG_NOISE_TEXT</text></svg>
            <noscript>Please enable JavaScript</noscript>
            <ul>
              <li>现金流折现校验通过</li>
              <li>Alpha-Beta ratio &gt; 1.25</li>
            </ul>
          </main>
          <aside>Sidebar ads</aside>
          <footer>© Internal Portal Footer</footer>
        </body>
        </html>
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            src_file = tmp / "captured" / "notion_page.html"
            src_file.parent.mkdir(parents=True)
            src_file.write_text(raw_html, encoding="utf-8")
            out_dir = tmp / "clean"

            results = self.mod.ingest_paths(
                [src_file],
                out_dir,
                captured_at="2026-11-25T09:00:00Z",
            )
            self.assertEqual(len(results), 1)
            out_path = Path(results[0]["output_path"])
            self.assertTrue(out_path.is_file())
            content = out_path.read_text(encoding="utf-8")

            # Frontmatter fields present
            self.assertTrue(content.startswith("---\n"))
            self.assertIn('source_url: "https://www.notion.so/team/q4-compliance-review"', content)
            self.assertIn('captured_at: "2026-11-25T09:00:00Z"', content)
            self.assertIn('title: "Q4 财务合规复盘 — Notion"', content)
            self.assertIn('domain: "www.notion.so"', content)

            # CJK and real content preserved
            self.assertIn("# Q4 财务合规复盘", content)
            self.assertIn("依据《民法典》第3条与 IFRS 15 收入确认口径，核对繁體中文與简体中文条目。", content)
            self.assertIn("- 现金流折现校验通过", content)
            self.assertIn("- Alpha-Beta ratio > 1.25", content)

            # Stripped elements completely absent
            for forbidden in (
                "do_not_index_js",
                "secret-css",
                "internal HTML comment",
                "Workspace Home",
                "SVG_NOISE_TEXT",
                "Please enable JavaScript",
                "Sidebar ads",
                "Internal Portal Footer",
            ):
                self.assertNotIn(forbidden, content)

    def test_secret_redaction_scrubs_all_tokens_headers_and_cookies(self) -> None:
        fake_bearer = "tok_live_bearer_998877665544332211"
        fake_apikey = "ak_internal_secret_887766554433"
        fake_cookie = "session_id=sess_secret_alpha999; csrftoken=csrf_beta888"
        fake_hf = "hf" + "_" + "AbCdEfGhIjKlMnOpQrStUvWxYz" + "0123456789"
        fake_ghp = "ghp" + "_" + "1234567890abcdef" + "ABCDEF1234567890ab"
        fake_sk = "sk" + "-proj-" + "9876543210abcdef" + "ABCDEF1234567890"
        fake_jwt = (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
            "eyJzdWIiOiJ1c2VyXzQyIiwicm9sZSI6ImFkbWluIn0."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        fake_query_token = "qtok_super_secret_55667788"

        sample_md = "\n".join(
            [
                "# Internal Debug Export",
                f"Authorization: Bearer {fake_bearer}",
                f"Authorization: ApiKey {fake_apikey}",
                f"ApiKey {fake_apikey}",
                f"Cookie: {fake_cookie}",
                '{"Cookie": "json_sid=json_secret_776655; pref=1"}',
                "Set-Cookie: sid=set_cookie_secret_443322; HttpOnly; Secure",
                f"HuggingFace token: {fake_hf}",
                f"GitHub PAT: {fake_ghp}",
                f"OpenAI key: {fake_sk}",
                f"JWT assertion: {fake_jwt}",
                f"Callback: https://wiki.internal.test/view?doc=7&access_token={fake_query_token}",
                "Safe Chinese note: 正常业务结论应完整保留。",
            ]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            src_md = tmp / "export.md"
            src_md.write_text(sample_md, encoding="utf-8")
            out_dir = tmp / "out"

            results = self.mod.ingest_paths(
                [src_md],
                out_dir,
                source_url=f"https://wiki.internal.test/page?token={fake_query_token}",
                captured_at="2026-11-25T09:05:00Z",
            )
            self.assertGreaterEqual(results[0]["redactions"], 9)
            rendered = Path(results[0]["output_path"]).read_text(encoding="utf-8")

            for secret in (
                fake_bearer,
                fake_apikey,
                "sess_secret_alpha999",
                "csrf_beta888",
                "json_secret_776655",
                "set_cookie_secret_443322",
                fake_hf,
                fake_ghp,
                fake_sk,
                fake_jwt,
                fake_query_token,
            ):
                self.assertNotIn(secret, rendered, f"Secret leaked into Markdown: {secret}")

            self.assertIn("<REDACTED>", rendered)
            self.assertIn("正常业务结论应完整保留。", rendered)

    def test_jsonl_batch_ingest_and_safe_filename_generation(self) -> None:
        batch_fake_pat = "ghp" + "_" + "AAAABBBBCCCCDDDD" + "EEEE1111222233334444"
        records = [
            {
                "source_url": "https://feishu.cn/docx/abc123",
                "title": "飞书产品评审",
                "captured_at": "2026-11-25T09:10:00Z",
                "html": "<article><h1>飞书评审</h1><p>核心指标达标</p><script>evil()</script></article>",
            },
            {
                "url": "https://confluence.internal/display/ENG/RFC",
                "title": "RFC-42 Architecture",
                "markdown": f"Auth header leaked: {batch_fake_pat}\n\nKeep this section.",
            },
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            jsonl_file = tmp / "batch.jsonl"
            jsonl_file.write_text(
                "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
                encoding="utf-8",
            )
            out_dir = tmp / "out"
            results = self.mod.ingest_paths([jsonl_file], out_dir)
            self.assertEqual(len(results), 2)

            md0 = Path(results[0]["output_path"]).read_text(encoding="utf-8")
            self.assertIn("核心指标达标", md0)
            self.assertNotIn("evil()", md0)
            self.assertIn('domain: "feishu.cn"', md0)

            md1 = Path(results[1]["output_path"]).read_text(encoding="utf-8")
            self.assertNotIn(batch_fake_pat, md1)
            self.assertIn("<REDACTED>", md1)
            self.assertIn("Keep this section.", md1)

    def test_crawl4ai_jsonl_fit_markdown_dict_and_example_adapter(self) -> None:
        crawl4ai_jwt = (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
            "eyJzdWIiOiJjcmF3bDRhaSJ9."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        records = [
            {
                "url": "https://docs.internal.example/runbooks/deploy",
                "metadata": {"title": "部署手册 — Crawl4AI"},
                "html": "<html><body><nav>noisy nav</nav><p>Raw fallback only</p></body></html>",
                "markdown": {
                    "raw_markdown": "noisy nav\n\n# 部署手册\n\n原始全文",
                    "fit_markdown": f"# 部署手册\n\n只保留核心正文，且脱敏 {crawl4ai_jwt}",
                },
            },
            {
                "source_url": "https://docs.internal.example/runbooks/raw-only",
                "markdown": {"raw_markdown": "# Raw Markdown Dict\n\nClean body."},
            },
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            jsonl_file = tmp / "crawl4ai_dump.jsonl"
            jsonl_file.write_text(
                "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
                encoding="utf-8",
            )
            out_dir = tmp / "clean"
            results = self.mod.ingest_paths([jsonl_file], out_dir)
            self.assertEqual(len(results), 2)

            md0 = Path(results[0]["output_path"]).read_text(encoding="utf-8")
            self.assertIn('title: "部署手册 — Crawl4AI"', md0)
            self.assertIn('domain: "docs.internal.example"', md0)
            self.assertIn("只保留核心正文", md0)
            self.assertNotIn("noisy nav", md0)
            self.assertNotIn("Raw fallback only", md0)
            self.assertNotIn(crawl4ai_jwt, md0)
            self.assertIn("<REDACTED>", md0)

            md1 = Path(results[1]["output_path"]).read_text(encoding="utf-8")
            self.assertIn("Clean body.", md1)

        # Verify examples/crawl4ai_auth_crawl.py helper contract without requiring crawl4ai installed
        example_path = REPO_ROOT / "examples" / "crawl4ai_auth_crawl.py"
        spec = importlib.util.spec_from_file_location("crawl4ai_auth_crawl", example_path)
        assert spec is not None and spec.loader is not None
        ex_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ex_mod)

        class _FakeMd:
            fit_markdown = "# Pruned Doc\n\nKey paragraph."
            raw_markdown = "Nav\n\n# Pruned Doc\n\nKey paragraph."

        class _FakeResult:
            url = "https://docs.example.com/private"
            metadata = {"title": "Private Guide"}
            markdown = _FakeMd()
            status_code = 200

        rec = ex_mod.crawl_result_to_record(
            _FakeResult(), prefer_fit=True, captured_at="2026-11-27T00:00:00Z"
        )
        self.assertEqual(rec["source_url"], "https://docs.example.com/private")
        self.assertEqual(rec["title"], "Private Guide")
        self.assertEqual(rec["markdown"], "# Pruned Doc\n\nKey paragraph.")
        with self.assertRaises(ValueError):
            ex_mod.validate_http_url("file:///etc/passwd")

    def test_loopback_url_guard_and_remote_https_policy(self) -> None:
        # Allowed loopback defaults
        for ok_url in (
            "http://127.0.0.1:9200",
            "http://127.0.0.1:9200/",
            "http://localhost:9200",
            "http://[::1]:9200",
            "https://127.0.0.1:9443",
        ):
            normalized = self.mod.validate_xerj_url(ok_url)
            self.assertFalse(normalized.endswith("/"))

        # Blocked by default
        for bad_url in (
            "http://192.168.1.50:9200",
            "https://xerj.example.com",
            "http://0.0.0.0:9200",
            "http://169.254.169.254",
            "http://localhost.evil.com:9200",
            "http://127.0.0.1.nip.io:9200",
            "ftp://127.0.0.1:9200",
            "http://user:pass@127.0.0.1:9200",
        ):
            with self.assertRaises(ValueError, msg=f"Should reject {bad_url}"):
                self.mod.validate_xerj_url(bad_url)

        # --allow-remote permits HTTPS only, never plaintext remote HTTP
        self.assertEqual(
            self.mod.validate_xerj_url("https://xerj.example.com/", allow_remote=True),
            "https://xerj.example.com",
        )
        with self.assertRaises(ValueError):
            self.mod.validate_xerj_url("http://xerj.example.com:9200", allow_remote=True)

    def test_path_traversal_symlink_escape_and_secret_file_rejection(self) -> None:
        with self.assertRaises(ValueError):
            self.mod.validate_safe_path("../escape")
        with self.assertRaises(ValueError):
            self.mod.validate_safe_path("/tmp/foo/../../etc/passwd")
        with self.assertRaises(ValueError):
            self.mod.validate_safe_path("nested\\..\\escape")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            outside = tmp / "outside.md"
            outside.write_text("outside secret", encoding="utf-8")

            in_dir = tmp / "in_dir"
            in_dir.mkdir()
            link = in_dir / "escape.md"
            link.symlink_to(outside)

            with self.assertRaises(ValueError):
                self.mod.ingest_paths([in_dir], tmp / "out")

            # Refuse ingesting .env or sessfork state.json directly
            env_file = tmp / ".env"
            env_file.write_text("API_KEY=supersecret", encoding="utf-8")
            with self.assertRaises(ValueError):
                self.mod.ingest_paths([env_file], tmp / "out")

            sess_dir = tmp / ".sessfork"
            sess_dir.mkdir()
            state_json = sess_dir / "state.json"
            state_json.write_text("{}", encoding="utf-8")
            with self.assertRaises(ValueError):
                self.mod.ingest_paths([state_json], tmp / "out")

            # Reject path traversal inside JSONL output_name
            bad_jsonl = tmp / "bad.jsonl"
            bad_jsonl.write_text(
                json.dumps({"output_name": "../escape.md", "markdown": "hi"}) + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                self.mod.ingest_paths([bad_jsonl], tmp / "out2")

            # Reject option-injection prefix
            with self.assertRaises(ValueError):
                self.mod.validate_prefix("--replace-prefix")

    def test_dry_run_default_vs_index_command_construction(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            src = tmp / "note.html"
            src.write_text("<html><title>Test Note</title><body><p>Hello XERJ</p></body></html>", encoding="utf-8")
            out_dir = tmp / "clean"

            # Default mode is dry-run: writes Markdown, NEVER invokes subprocess.run
            with mock.patch.object(self.mod.subprocess, "run") as mock_run:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = self.mod.main([str(src), "--out", str(out_dir), "--json"])
                self.assertEqual(rc, 0)
                mock_run.assert_not_called()
                summary = json.loads(buf.getvalue())
                self.assertTrue(summary["dry_run"])
                self.assertFalse(summary["indexed"])
                self.assertEqual(summary["documents_written"], 1)

            # --index invokes xerj --url <url> autoindex <out_dir> --prefix <prefix> with shell=False
            fake_completed = subprocess.CompletedProcess(
                args=["xerj"], returncode=0, stdout="ok: indexed 1 doc\n", stderr=""
            )
            with mock.patch.object(self.mod.subprocess, "run", return_value=fake_completed) as mock_run:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = self.mod.main(
                        [
                            str(src),
                            "--out",
                            str(out_dir),
                            "--index",
                            "--url",
                            "http://127.0.0.1:9200",
                            "--prefix",
                            "authnotes",
                            "--json",
                        ]
                    )
                self.assertEqual(rc, 0)
                mock_run.assert_called_once()
                call_args, call_kwargs = mock_run.call_args
                self.assertEqual(
                    call_args[0],
                    [
                        "xerj",
                        "--url",
                        "http://127.0.0.1:9200",
                        "autoindex",
                        str(out_dir.resolve()),
                        "--prefix",
                        "authnotes",
                    ],
                )
                self.assertFalse(call_kwargs.get("shell", False))
                self.assertEqual(call_kwargs["env"].get("XERJ_DISABLE_FEEDBACK"), "true")
                summary = json.loads(buf.getvalue())
                self.assertFalse(summary["dry_run"])
                self.assertTrue(summary["indexed"])

            # Mutually exclusive --dry-run and --index rejected
            with redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as cm:
                    self.mod.main([str(src), "--out", str(out_dir), "--dry-run", "--index"])
            self.assertNotEqual(cm.exception.code, 0)

            # Traversal rejected via CLI
            with redirect_stderr(io.StringIO()):
                rc_bad = self.mod.main([str(src), "--out", str(tmp / ".." / "escape")])
            self.assertNotEqual(rc_bad, 0)

    @unittest.skipUnless(_find_xerj_binary() is not None, "xerj binary not installed")
    def test_live_isolated_temp_xerj_e2e_smoke(self) -> None:
        """Start an isolated temporary XERJ node on a free random port, index, search, and tear down cleanly."""
        xerj_bin = _find_xerj_binary()
        assert xerj_bin is not None
        port = _find_three_consecutive_free_ports()
        self.assertNotIn(port, (9200, 9204))

        unique_token = f"SESSFORK_E2E_TOKEN_{os.getpid()}_{int(time.time())}"
        secret_sentinel = "ghp" + "_" + "ZZZZ9999YYYY8888" + "XXXX7777WWWW66665555"

        with tempfile.TemporaryDirectory(prefix="sessfork-xerj-e2e-") as tmpdir:
            tmp = Path(tmpdir)
            data_dir = tmp / "xerj-data"
            state_dir = tmp / "xerj-state"
            src_dir = tmp / "captured"
            out_dir = tmp / "clean-md"
            src_dir.mkdir()

            sample_html = f"""<!doctype html>
            <html>
            <head>
              <title>孤岛实测文档 {unique_token}</title>
              <link rel="canonical" href="https://notion.so/demo/isolated-e2e">
              <script>const leak = "{secret_sentinel}";</script>
            </head>
            <body>
              <main>
                <h1>隔离节点检索验证</h1>
                <p>唯一验证标识 {unique_token} 与中文段落必须可检索。</p>
                <p>Authorization: Bearer bearer_should_be_scrubbed_99887766</p>
                <p>Leaked token: {secret_sentinel}</p>
              </main>
            </body>
            </html>
            """
            (src_dir / "page.html").write_text(sample_html, encoding="utf-8")

            env = dict(os.environ)
            env["XERJ_DISABLE_FEEDBACK"] = "true"
            env["XERJ_NO_AUTOSTART"] = "1"

            proc = subprocess.Popen(
                [
                    xerj_bin,
                    "--bind",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--data-dir",
                    str(data_dir),
                    "--insecure",
                    "--embed-mode",
                    "lexical",
                    "--disable-feedback",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
            try:
                base_url = f"http://127.0.0.1:{port}"
                deadline = time.monotonic() + 15.0
                healthy = False
                while time.monotonic() < deadline:
                    if proc.poll() is not None:
                        err = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
                        self.fail(f"Isolated xerj exited early ({proc.returncode}): {err}")
                    try:
                        status, _ = _loopback_http_json(f"{base_url}/_cluster/health", timeout=1.0)
                        if status == 200:
                            healthy = True
                            break
                    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
                        time.sleep(0.15)
                self.assertTrue(healthy, f"Isolated xerj on {base_url} did not become healthy in time")

                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = self.mod.main(
                        [
                            str(src_dir),
                            "--out",
                            str(out_dir),
                            "--index",
                            "--url",
                            base_url,
                            "--prefix",
                            "authsmoke",
                            "--state-dir",
                            str(state_dir),
                            "--xerj-bin",
                            xerj_bin,
                            "--json",
                        ]
                    )
                self.assertEqual(rc, 0, f"ingest_to_xerj.py failed: {buf.getvalue()}")
                summary = json.loads(buf.getvalue())
                self.assertTrue(summary["indexed"])
                self.assertEqual(summary["documents_written"], 1)

                # Search the isolated XERJ node over HTTP
                status, search_resp = _loopback_http_json(
                    f"{base_url}/authsmoke-*/_search",
                    method="POST",
                    payload={"size": 5, "query": {"query_string": {"query": unique_token}}},
                    timeout=5.0,
                )
                self.assertEqual(status, 200)
                hits = search_resp.get("hits", {}).get("hits", [])
                self.assertGreaterEqual(len(hits), 1, f"Expected >=1 hit, got: {search_resp}")
                serialized_hits = json.dumps(hits, ensure_ascii=False)
                self.assertIn(unique_token, serialized_hits)
                self.assertIn("唯一验证标识", serialized_hits)
                self.assertNotIn(secret_sentinel, serialized_hits)
                self.assertNotIn("bearer_should_be_scrubbed_99887766", serialized_hits)
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5.0)
                if proc.stdout is not None:
                    proc.stdout.close()
                if proc.stderr is not None:
                    proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
