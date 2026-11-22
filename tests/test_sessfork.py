"""End-to-end unit tests for sessfork (zero network, zero OS keychain prompts)."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sqlite3
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from sessfork.chrome import (
    CHROMIUM_EPOCH_DELTA_SECONDS,
    BrowserProfileError,
    extract_cookies,
    normalize_domain_filter,
)
from sessfork.cli import main
from sessfork.crypto import (
    MACOS_ITERATIONS,
    _read_macos_keychain,
    aes_128_cbc_encrypt_pkcs7,
    decrypt_chromium_cookie,
    derive_aes_key,
    get_safe_storage_key,
)
from sessfork.mcp_setup import (
    DEFAULT_PLAYWRIGHT_MCP_PKG,
    MCP_SERVER_NAME,
    build_playwright_mcp_entry,
    ensure_mcp_configured,
    find_playwright_headless_shell,
    inspect_mcp_headless_shell,
    install_agent_skills,
)
from sessfork.state import (
    clear_cookies,
    hot_reload_state_path,
    list_state_domains,
    load_storage_state,
    summarize_state,
    upsert_cookies,
    upsert_session_state,
)


def _unix_to_chromium_us(unix_ts: float) -> int:
    return int((unix_ts + CHROMIUM_EPOCH_DELTA_SECONDS) * 1_000_000)


def _encrypt_cookie_blob(value: str, host_key: str, aes_key: bytes, v24_hash: bool = True) -> bytes:
    prefix = hashlib.sha256(host_key.encode("utf-8")).digest() if v24_hash else b""
    plaintext = prefix + value.encode("utf-8")
    return b"v10" + aes_128_cbc_encrypt_pkcs7(plaintext, aes_key)


class SessforkTestSuite(unittest.TestCase):
    def setUp(self) -> None:
        self.aes_key = derive_aes_key(b"unit-test-key-material", MACOS_ITERATIONS)

    def test_crypto_roundtrip_v24_and_legacy(self) -> None:
        host = ".example.com"
        cookie_val = "sample_cookie_alpha_123"

        enc_v24 = _encrypt_cookie_blob(cookie_val, host, self.aes_key, v24_hash=True)
        dec_v24 = decrypt_chromium_cookie(enc_v24, "", host, self.aes_key, db_version=24)
        self.assertEqual(dec_v24, cookie_val)

        enc_v20 = _encrypt_cookie_blob(cookie_val, host, self.aes_key, v24_hash=False)
        dec_v20 = decrypt_chromium_cookie(enc_v20, "", host, self.aes_key, db_version=20)
        self.assertEqual(dec_v20, cookie_val)

        other_key = derive_aes_key(b"different-key-material", MACOS_ITERATIONS)
        self.assertIsNone(decrypt_chromium_cookie(enc_v24, "", host, other_key, db_version=24))

    def test_wal_locked_sqlite_extraction_and_samesite_normalization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "Default" / "Cookies"
            db_path.parent.mkdir(parents=True)

            live_conn = sqlite3.connect(str(db_path))
            try:
                live_conn.execute("PRAGMA journal_mode=WAL;")
                live_conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);")
                live_conn.execute("INSERT INTO meta VALUES ('version', '24');")
                live_conn.execute(
                    """
                    CREATE TABLE cookies (
                        host_key TEXT,
                        name TEXT,
                        value TEXT,
                        encrypted_value BLOB,
                        path TEXT,
                        expires_utc INTEGER,
                        is_secure INTEGER,
                        is_httponly INTEGER,
                        has_expires INTEGER,
                        samesite INTEGER
                    );
                    """
                )
                future_exp = _unix_to_chromium_us(time.time() + 7200)
                past_exp = _unix_to_chromium_us(time.time() - 7200)

                rows = [
                    (
                        ".github.com",
                        "logged_in_flag",
                        "",
                        _encrypt_cookie_blob("val_main", ".github.com", self.aes_key),
                        "/",
                        future_exp,
                        1,
                        1,
                        1,
                        2,
                    ),
                    (
                        "gist.github.com",
                        "sub_pref",
                        "",
                        _encrypt_cookie_blob("val_sub", "gist.github.com", self.aes_key),
                        "/",
                        0,
                        0,
                        1,
                        0,
                        0,
                    ),
                    (
                        ".github.com",
                        "expired_item",
                        "",
                        _encrypt_cookie_blob("val_old", ".github.com", self.aes_key),
                        "/",
                        past_exp,
                        1,
                        0,
                        1,
                        1,
                    ),
                    (
                        ".unrelated.org",
                        "other",
                        "",
                        _encrypt_cookie_blob("val_skip", ".unrelated.org", self.aes_key),
                        "/",
                        future_exp,
                        1,
                        0,
                        1,
                        1,
                    ),
                ]
                live_conn.executemany("INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);", rows)
                live_conn.commit()

                cookies, resolved = extract_cookies(
                    domains=["https://github.com/settings"],
                    db_path=db_path,
                    aes_key=self.aes_key,
                )
                self.assertEqual(resolved, db_path.resolve())
                self.assertEqual(len(cookies), 2)

                by_name = {c["name"]: c for c in cookies}
                self.assertEqual(by_name["logged_in_flag"]["value"], "val_main")
                self.assertTrue(by_name["logged_in_flag"]["httpOnly"])
                self.assertEqual(by_name["logged_in_flag"]["sameSite"], "Strict")

                self.assertEqual(by_name["sub_pref"]["value"], "val_sub")
                self.assertEqual(by_name["sub_pref"]["expires"], -1)
                self.assertEqual(by_name["sub_pref"]["sameSite"], "Lax")
            finally:
                live_conn.close()

    def test_state_atomic_permissions_merge_and_redacted_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "store" / "state.json"
            c1 = {
                "name": "sid",
                "value": "RAW_COOKIE_PAYLOAD_ONE",
                "domain": ".github.com",
                "path": "/",
                "expires": time.time() + 3600,
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            }
            c2 = {
                "name": "pref",
                "value": "RAW_COOKIE_PAYLOAD_TWO",
                "domain": ".x.com",
                "path": "/",
                "expires": -1,
                "httpOnly": True,
                "secure": True,
                "sameSite": "None",
            }
            upsert_cookies([c1], state_path=state_file)
            saved, added, total = upsert_cookies([c2], state_path=state_file)
            self.assertEqual(added, 1)
            self.assertEqual(total, 2)

            mode = stat.S_IMODE(os.stat(saved).st_mode)
            self.assertEqual(mode, 0o600)

            summary = summarize_state(state_file)
            serialized = json.dumps(summary)
            self.assertNotIn("RAW_COOKIE_PAYLOAD_ONE", serialized)
            self.assertNotIn("RAW_COOKIE_PAYLOAD_TWO", serialized)
            self.assertEqual(summary["total_cookies"], 2)

            removed, remaining = clear_cookies(["x.com"], state_path=state_file)
            self.assertEqual(removed, 1)
            self.assertEqual(remaining, 1)
            self.assertEqual(load_storage_state(state_file)["cookies"][0]["domain"], ".github.com")

    def test_mcp_and_skill_idempotent_setup_and_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox_home = Path(tmp)
            state_file = sandbox_home / "state.json"
            agy_cfg = sandbox_home / ".gemini" / "config" / "mcp_config.json"
            agy_cfg.parent.mkdir(parents=True)
            agy_cfg.write_text(
                json.dumps({"mcpServers": {"existing-tool": {"command": "echo", "args": ["ok"]}}}),
                encoding="utf-8",
            )

            updated = ensure_mcp_configured(
                ["antigravity", "claude"],
                state_path=state_file,
                home=sandbox_home,
                npx_cmd="/opt/homebrew/bin/npx",
            )
            self.assertEqual(len(updated), 2)

            parsed = json.loads(agy_cfg.read_text(encoding="utf-8"))
            self.assertIn("existing-tool", parsed["mcpServers"])
            self.assertIn(MCP_SERVER_NAME, parsed["mcpServers"])
            self.assertNotIn("type", parsed["mcpServers"][MCP_SERVER_NAME])
            self.assertIn("--isolated", parsed["mcpServers"][MCP_SERVER_NAME]["args"])
            self.assertEqual(parsed["mcpServers"][MCP_SERVER_NAME]["command"], "/opt/homebrew/bin/npx")
            mcp_args = parsed["mcpServers"][MCP_SERVER_NAME]["args"]
            expected_out = state_file.resolve().parent / "playwright-output"
            self.assertIn(f"--output-dir={expected_out}", mcp_args)
            self.assertTrue(expected_out.is_dir())

            updated_again = ensure_mcp_configured(
                ["antigravity", "claude"],
                state_path=state_file,
                home=sandbox_home,
                npx_cmd="/opt/homebrew/bin/npx",
            )
            self.assertEqual(updated_again, [])

            skills = install_agent_skills(home=sandbox_home)
            self.assertEqual(len(skills), 2)

            db_path = sandbox_home / "Cookies"
            conn = sqlite3.connect(str(db_path))
            conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);")
            conn.execute("INSERT INTO meta VALUES ('version', '24');")
            conn.execute(
                """
                CREATE TABLE cookies (
                    host_key TEXT, name TEXT, value TEXT, encrypted_value BLOB,
                    path TEXT, expires_utc INTEGER, is_secure INTEGER,
                    is_httponly INTEGER, has_expires INTEGER, samesite INTEGER
                );
                """
            )
            conn.execute(
                "INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
                (
                    ".github.com",
                    "logged_in",
                    "",
                    _encrypt_cookie_blob("yes", ".github.com", self.aes_key),
                    "/",
                    _unix_to_chromium_us(time.time() + 3600),
                    1,
                    0,
                    1,
                    1,
                ),
            )
            conn.commit()
            conn.close()

            with mock.patch("sessfork.chrome.get_safe_storage_key", return_value=self.aes_key):
                rc = main(
                    [
                        "github.com",
                        "--db-path",
                        str(db_path),
                        "--state-file",
                        str(state_file),
                        "--target",
                        "none",
                    ]
                )
                self.assertEqual(rc, 0)
                loaded = load_storage_state(state_file)
                self.assertEqual(len(loaded["cookies"]), 1)
                self.assertEqual(loaded["cookies"][0]["value"], "yes")

    def test_normalize_domain_filter(self) -> None:
        self.assertEqual(normalize_domain_filter("https://gist.github.com/demo"), "gist.github.com")
        self.assertEqual(normalize_domain_filter(".x.com"), "x.com")
        self.assertEqual(normalize_domain_filter("wasabihu.github.io"), "wasabihu.github.io")
        self.assertEqual(normalize_domain_filter("localhost:3000"), "localhost")
        self.assertEqual(normalize_domain_filter("127.0.0.1:8080"), "127.0.0.1")

    def test_security_hardening_guards(self) -> None:
        for bad in ("com", "co.uk", "github.io", "vercel.app", "github.com;id", "foo bar", "ftp://github.com"):
            with self.assertRaises(BrowserProfileError, msg=f"Expected rejection for {bad!r}"):
                normalize_domain_filter(bad)

        with tempfile.TemporaryDirectory() as tmp:
            sandbox_home = Path(tmp)
            state_file = sandbox_home / ".sessfork" / "state.json"
            ensure_mcp_configured(
                ["claude"],
                state_path=state_file,
                home=sandbox_home,
                npx_cmd="/opt/homebrew/bin/npx",
            )
            claude_cfg = sandbox_home / ".claude.json"
            self.assertEqual(stat.S_IMODE(os.stat(claude_cfg).st_mode), 0o600)
            out_dir = sandbox_home / ".sessfork" / "playwright-output"
            self.assertEqual(stat.S_IMODE(os.stat(out_dir).st_mode), 0o700)
            parsed = json.loads(claude_cfg.read_text(encoding="utf-8"))
            args = parsed["mcpServers"][MCP_SERVER_NAME]["args"]
            self.assertIn(DEFAULT_PLAYWRIGHT_MCP_PKG, args)
            self.assertNotIn("@playwright/mcp@latest", args)

            real_db = sandbox_home / "CookiesReal"
            real_db.write_bytes(b"")
            sym_db = sandbox_home / "CookiesLink"
            os.symlink(real_db, sym_db)
            with self.assertRaises(BrowserProfileError):
                extract_cookies(["github.com"], db_path=sym_db, aes_key=self.aes_key)

    def test_auto_target_detection_and_one_shot_skill_install(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox_home = Path(tmp)
            state_file = sandbox_home / "state.json"
            (sandbox_home / ".cursor").mkdir()
            (sandbox_home / ".claude").mkdir()

            updated = ensure_mcp_configured(
                ["auto"],
                state_path=state_file,
                home=sandbox_home,
                npx_cmd="/opt/homebrew/bin/npx",
            )
            self.assertEqual(len(updated), 3)
            self.assertTrue((sandbox_home / ".gemini" / "config" / "mcp_config.json").is_file())
            self.assertTrue((sandbox_home / ".claude.json").is_file())
            self.assertTrue((sandbox_home / ".cursor" / "mcp.json").is_file())

            skills = install_agent_skills(home=sandbox_home)
            self.assertEqual(len(skills), 3)
            self.assertTrue((sandbox_home / ".claude" / "skills" / "sessfork" / "SKILL.md").is_file())

    def test_derived_key_cache_permissions_and_single_prompt_on_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            keys_file = Path(tmp) / "keys.json"
            # Default: cache is OFF -> keychain read every time, nothing written to disk
            with mock.patch("sessfork.crypto._read_macos_keychain", return_value=b"mock-chrome-secret") as m_kc:
                get_safe_storage_key("chrome", platform="darwin", cache_path=keys_file)
                get_safe_storage_key("chrome", platform="darwin", cache_path=keys_file)
                self.assertEqual(m_kc.call_count, 2)
                self.assertFalse(keys_file.exists())

            # Opt-in: cache is ON -> single keychain read, 0600 file, raw passphrase never stored
            with mock.patch("sessfork.crypto._read_macos_keychain", return_value=b"mock-chrome-secret") as m_kc:
                key1 = get_safe_storage_key("chrome", platform="darwin", cache_path=keys_file, use_cache=True)
                self.assertEqual(m_kc.call_count, 1)
                self.assertEqual(len(key1), 16)
                self.assertEqual(stat.S_IMODE(os.stat(keys_file).st_mode), 0o600)

                key2 = get_safe_storage_key("chrome", platform="darwin", cache_path=keys_file, use_cache=True)
                self.assertEqual(key2, key1)
                self.assertEqual(m_kc.call_count, 1)
                self.assertNotIn("mock-chrome-secret", keys_file.read_text(encoding="utf-8"))

        seen_cmds: list[list[str]] = []

        def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
            seen_cmds.append(list(cmd))
            if "-w" in cmd:
                return subprocess.CompletedProcess(cmd, 128, b"", b"User canceled the operation")
            return subprocess.CompletedProcess(cmd, 0, b"keychain metadata", b"")

        with mock.patch("sessfork.crypto.subprocess.run", side_effect=fake_run):
            secret = _read_macos_keychain("Chrome Safe Storage", "Chrome")
            self.assertIsNone(secret)
            w_calls = [cmd for cmd in seen_cmds if "-w" in cmd]
            self.assertEqual(len(w_calls), 1)

    def test_lazy_keychain_skip_and_stale_key_self_healing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plain_db = tmp_dir / "PlainCookies"
            conn = sqlite3.connect(str(plain_db))
            conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);")
            conn.execute("INSERT INTO meta VALUES ('version', '24');")
            conn.execute(
                """
                CREATE TABLE cookies (
                    host_key TEXT, name TEXT, value TEXT, encrypted_value BLOB,
                    path TEXT, expires_utc INTEGER, is_secure INTEGER,
                    is_httponly INTEGER, has_expires INTEGER, samesite INTEGER
                );
                """
            )
            future_exp = _unix_to_chromium_us(time.time() + 3600)
            conn.execute(
                "INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
                (".linear.app", "plain_flag", "ok", b"", "/", future_exp, 1, 0, 1, 1),
            )
            conn.commit()
            conn.close()

            with mock.patch("sessfork.chrome.get_safe_storage_key") as m_key:
                cookies, _ = extract_cookies(["linear.app"], db_path=plain_db)
                self.assertEqual(m_key.call_count, 0)
                self.assertEqual(len(cookies), 1)
                self.assertEqual(cookies[0]["value"], "ok")

            enc_db = tmp_dir / "EncCookies"
            real_key = self.aes_key
            wrong_key = derive_aes_key(b"stale-wrong-key", MACOS_ITERATIONS)
            conn2 = sqlite3.connect(str(enc_db))
            conn2.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);")
            conn2.execute("INSERT INTO meta VALUES ('version', '24');")
            conn2.execute(
                """
                CREATE TABLE cookies (
                    host_key TEXT, name TEXT, value TEXT, encrypted_value BLOB,
                    path TEXT, expires_utc INTEGER, is_secure INTEGER,
                    is_httponly INTEGER, has_expires INTEGER, samesite INTEGER
                );
                """
            )
            conn2.execute(
                "INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
                (
                    ".github.com",
                    "session_id",
                    "",
                    _encrypt_cookie_blob("fresh_val", ".github.com", real_key),
                    "/",
                    future_exp,
                    1,
                    1,
                    1,
                    1,
                ),
            )
            conn2.commit()
            conn2.close()

            # Without the opt-in cache, a wrong key is NOT retried (no pointless second prompt)
            with mock.patch("sessfork.chrome.get_safe_storage_key", side_effect=[wrong_key]) as m_once:
                cookies_once, _ = extract_cookies(["github.com"], db_path=enc_db)
                self.assertEqual(m_once.call_count, 1)
                self.assertEqual(m_once.call_args_list[0].kwargs["use_cache"], False)
                self.assertEqual(cookies_once, [])

            # With the opt-in cache, a stale cached key self-heals via one forced refresh
            with mock.patch("sessfork.chrome.get_safe_storage_key", side_effect=[wrong_key, real_key]) as m_heal:
                cookies2, _ = extract_cookies(["github.com"], db_path=enc_db, cache_key=True)
                self.assertEqual(m_heal.call_count, 2)
                self.assertEqual(m_heal.call_args_list[0].kwargs["refresh"], False)
                self.assertEqual(m_heal.call_args_list[1].kwargs["refresh"], True)
                self.assertEqual(len(cookies2), 1)
                self.assertEqual(cookies2[0]["value"], "fresh_val")

    def test_hot_reload_mirror_and_refresh_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            empty_state = tmp_dir / "empty" / "state.json"
            self.assertEqual(
                main(["--refresh", "--state-file", str(empty_state), "--target", "none"]),
                1,
            )

            state_file = tmp_dir / "store" / "state.json"
            future_exp = time.time() + 3600
            upsert_session_state(
                new_cookies=[
                    {
                        "name": "old_sid",
                        "value": "old_val",
                        "domain": ".github.com",
                        "path": "/",
                        "expires": future_exp,
                        "httpOnly": True,
                        "secure": True,
                        "sameSite": "Lax",
                    },
                    {
                        "name": "sub_sid",
                        "value": "sub_val",
                        "domain": "gist.github.com",
                        "path": "/",
                        "expires": -1,
                        "httpOnly": False,
                        "secure": True,
                        "sameSite": "Lax",
                    },
                ],
                new_origins=[
                    {
                        "origin": "https://app.linear.app",
                        "localStorage": [{"name": "token", "value": "jwt_123"}],
                    }
                ],
                state_path=state_file,
            )

            mirror = hot_reload_state_path(state_file)
            self.assertTrue(mirror.is_file())
            self.assertEqual(stat.S_IMODE(os.stat(mirror).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(mirror.parent).st_mode), 0o700)
            self.assertEqual(mirror.read_text(encoding="utf-8"), state_file.read_text(encoding="utf-8"))
            self.assertEqual(list_state_domains(state_file), ["app.linear.app", "github.com"])
            self.assertEqual(summarize_state(state_file)["hot_reload_path"], str(mirror))

            db_path = tmp_dir / "Cookies"
            conn = sqlite3.connect(str(db_path))
            conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);")
            conn.execute("INSERT INTO meta VALUES ('version', '24');")
            conn.execute(
                """
                CREATE TABLE cookies (
                    host_key TEXT, name TEXT, value TEXT, encrypted_value BLOB,
                    path TEXT, expires_utc INTEGER, is_secure INTEGER,
                    is_httponly INTEGER, has_expires INTEGER, samesite INTEGER
                );
                """
            )
            conn.execute(
                "INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
                (
                    ".github.com",
                    "old_sid",
                    "",
                    _encrypt_cookie_blob("refreshed_val", ".github.com", self.aes_key),
                    "/",
                    _unix_to_chromium_us(time.time() + 7200),
                    1,
                    1,
                    1,
                    1,
                ),
            )
            conn.commit()
            conn.close()

            with mock.patch("sessfork.chrome.get_safe_storage_key", return_value=self.aes_key):
                rc = main(
                    [
                        "--refresh",
                        "--no-local-storage",
                        "--db-path",
                        str(db_path),
                        "--state-file",
                        str(state_file),
                        "--target",
                        "none",
                    ]
                )
                self.assertEqual(rc, 0)

            reloaded = load_storage_state(mirror)
            by_name = {c["name"]: c["value"] for c in reloaded["cookies"]}
            self.assertEqual(by_name["old_sid"], "refreshed_val")

    def test_find_playwright_headless_shell_revisions_arches_and_overrides(self) -> None:
        def _make_exec(path: Path) -> Path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            os.chmod(path, 0o755)
            return path

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "ms-playwright"
            cache.mkdir()

            # Lower numeric revision (999 > 1254 lexicographically, must NOT beat 1254)
            _make_exec(cache / "chromium_headless_shell-999" / "chrome-headless-shell-mac-arm64" / "chrome-headless-shell")
            # Target highest valid revision 1254
            mac_arm = _make_exec(
                cache / "chromium_headless_shell-1254" / "chrome-headless-shell-mac-arm64" / "chrome-headless-shell"
            )
            mac_x64 = _make_exec(
                cache / "chromium_headless_shell-1254" / "chrome-headless-shell-mac-x64" / "chrome-headless-shell"
            )
            # Higher revisions that must be skipped: missing binary, non-executable, symlinked binary, symlinked dir
            (cache / "chromium_headless_shell-1300" / "chrome-headless-shell-mac-arm64").mkdir(parents=True)
            non_exec = cache / "chromium_headless_shell-1400" / "chrome-headless-shell-mac-arm64" / "chrome-headless-shell"
            non_exec.parent.mkdir(parents=True)
            non_exec.write_text("not executable", encoding="utf-8")
            os.chmod(non_exec, 0o644)

            sym_bin_dir = cache / "chromium_headless_shell-1500" / "chrome-headless-shell-mac-arm64"
            sym_bin_dir.mkdir(parents=True)
            os.symlink(mac_arm, sym_bin_dir / "chrome-headless-shell")

            outside_arch = Path(tmp) / "outside_arch"
            _make_exec(outside_arch / "chrome-headless-shell")
            sym_arch_rev = cache / "chromium_headless_shell-1550"
            sym_arch_rev.mkdir(parents=True)
            os.symlink(outside_arch, sym_arch_rev / "chrome-headless-shell-mac-arm64")

            outside_rev = Path(tmp) / "outside_rev"
            _make_exec(outside_rev / "chrome-headless-shell-mac-arm64" / "chrome-headless-shell")
            os.symlink(outside_rev, cache / "chromium_headless_shell-1600")

            # Non-headless-shell browser directory must be ignored
            _make_exec(cache / "chromium-1999" / "chrome-headless-shell-mac-arm64" / "chrome-headless-shell")

            # Linux cache layouts
            linux_cache = Path(tmp) / "linux-playwright"
            linux_x64 = _make_exec(
                linux_cache / "chromium_headless_shell-1200" / "chrome-headless-shell-linux64" / "chrome-headless-shell"
            )
            linux_arm = _make_exec(
                linux_cache / "chromium_headless_shell-1200" / "chrome-headless-shell-linux-arm64" / "chrome-headless-shell"
            )
            legacy_linux_cache = Path(tmp) / "legacy-linux-playwright"
            legacy_linux = _make_exec(
                legacy_linux_cache / "chromium_headless_shell-1100" / "chrome-linux" / "headless_shell"
            )

            clean_env = {
                k: v
                for k, v in os.environ.items()
                if k not in ("SESSFORK_BROWSER_EXECUTABLE_PATH", "PLAYWRIGHT_BROWSERS_PATH")
            }
            with mock.patch.dict(os.environ, clean_env, clear=True):
                self.assertEqual(
                    find_playwright_headless_shell(cache_dir=cache, platform="darwin", machine="arm64"),
                    mac_arm.resolve(),
                )
                self.assertEqual(
                    find_playwright_headless_shell(cache_dir=cache, platform="darwin", machine="x86_64"),
                    mac_x64.resolve(),
                )
                self.assertEqual(
                    find_playwright_headless_shell(cache_dir=linux_cache, platform="linux", machine="x86_64"),
                    linux_x64.resolve(),
                )
                self.assertEqual(
                    find_playwright_headless_shell(cache_dir=linux_cache, platform="linux", machine="aarch64"),
                    linux_arm.resolve(),
                )
                self.assertEqual(
                    find_playwright_headless_shell(cache_dir=legacy_linux_cache, platform="linux", machine="x86_64"),
                    legacy_linux.resolve(),
                )

                # PLAYWRIGHT_BROWSERS_PATH env var discovery when cache_dir is omitted
                with mock.patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": str(cache)}):
                    self.assertEqual(
                        find_playwright_headless_shell(platform="darwin", machine="arm64"),
                        mac_arm.resolve(),
                    )

                # Explicit disable via "system" or "none"
                with mock.patch.dict(os.environ, {"SESSFORK_BROWSER_EXECUTABLE_PATH": "system"}):
                    self.assertIsNone(
                        find_playwright_headless_shell(cache_dir=cache, platform="darwin", machine="arm64")
                    )
                with mock.patch.dict(os.environ, {"SESSFORK_BROWSER_EXECUTABLE_PATH": "NONE"}):
                    self.assertIsNone(
                        find_playwright_headless_shell(cache_dir=cache, platform="darwin", machine="arm64")
                    )

                # Explicit custom binary path override
                custom_bin = _make_exec(Path(tmp) / "custom-browser")
                with mock.patch.dict(os.environ, {"SESSFORK_BROWSER_EXECUTABLE_PATH": str(custom_bin)}):
                    self.assertEqual(
                        find_playwright_headless_shell(cache_dir=cache, platform="linux", machine="x86_64"),
                        custom_bin.resolve(),
                    )

                # Nonexistent custom path returns None cleanly without error
                with mock.patch.dict(os.environ, {"SESSFORK_BROWSER_EXECUTABLE_PATH": str(Path(tmp) / "missing")}):
                    self.assertIsNone(
                        find_playwright_headless_shell(cache_dir=cache, platform="darwin", machine="arm64")
                    )

                # Default OS cache discovery: real_user_home() priority, sandbox $HOME fallback,
                # SESSFORK_HOME isolation, and PLAYWRIGHT_BROWSERS_PATH="0" fallback to OS default cache
                real_home = Path(tmp) / "real-home"
                sandbox_home = Path(tmp) / "sandbox-home"
                empty_home = Path(tmp) / "empty-home"
                for d in (real_home, sandbox_home, empty_home):
                    d.mkdir()
                real_mac_bin = _make_exec(
                    real_home
                    / "Library"
                    / "Caches"
                    / "ms-playwright"
                    / "chromium_headless_shell-1240"
                    / "chrome-headless-shell-mac-arm64"
                    / "chrome-headless-shell"
                )
                sandbox_mac_bin = _make_exec(
                    sandbox_home
                    / "Library"
                    / "Caches"
                    / "ms-playwright"
                    / "chromium_headless_shell-1254"
                    / "chrome-headless-shell-mac-arm64"
                    / "chrome-headless-shell"
                )
                real_linux_bin = _make_exec(
                    real_home
                    / ".cache"
                    / "ms-playwright"
                    / "chromium_headless_shell-1210"
                    / "chrome-headless-shell-linux64"
                    / "chrome-headless-shell"
                )

                base_env = {
                    k: v
                    for k, v in clean_env.items()
                    if k not in ("HOME", "SESSFORK_HOME")
                }
                base_env["HOME"] = str(sandbox_home)
                with mock.patch.dict(os.environ, base_env, clear=True):
                    # real_user_home() takes priority over distinct sandbox $HOME when both have a shell
                    with mock.patch("sessfork.mcp_setup.real_user_home", return_value=real_home):
                        self.assertEqual(
                            find_playwright_headless_shell(platform="darwin", machine="arm64"),
                            real_mac_bin.resolve(),
                        )
                        # PLAYWRIGHT_BROWSERS_PATH="0" falls back to OS default cache (macOS & Linux)
                        with mock.patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": "0"}):
                            self.assertEqual(
                                find_playwright_headless_shell(platform="darwin", machine="arm64"),
                                real_mac_bin.resolve(),
                            )
                            self.assertEqual(
                                find_playwright_headless_shell(platform="linux", machine="x86_64"),
                                real_linux_bin.resolve(),
                            )

                    # When real_user_home() has no cache and SESSFORK_HOME is unset, falls back to sandbox $HOME
                    with mock.patch("sessfork.mcp_setup.real_user_home", return_value=empty_home):
                        self.assertEqual(
                            find_playwright_headless_shell(platform="darwin", machine="arm64"),
                            sandbox_mac_bin.resolve(),
                        )

                    # When SESSFORK_HOME is explicitly set, do NOT leak/fall back to sandbox $HOME
                    with mock.patch.dict(os.environ, {"SESSFORK_HOME": str(empty_home)}):
                        self.assertIsNone(
                            find_playwright_headless_shell(platform="darwin", machine="arm64")
                        )

    def test_build_playwright_mcp_entry_headless_shell_pinning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            state_file = tmp_dir / "state.json"
            cache = tmp_dir / "ms-playwright"
            mac_bin = cache / "chromium_headless_shell-1254" / "chrome-headless-shell-mac-arm64" / "chrome-headless-shell"
            mac_bin.parent.mkdir(parents=True)
            mac_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            os.chmod(mac_bin, 0o755)

            linux_bin = cache / "chromium_headless_shell-1254" / "chrome-headless-shell-linux64" / "chrome-headless-shell"
            linux_bin.parent.mkdir(parents=True)
            linux_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            os.chmod(linux_bin, 0o755)

            empty_cache = tmp_dir / "empty-cache"
            empty_cache.mkdir()

            clean_env = {
                k: v
                for k, v in os.environ.items()
                if k not in ("SESSFORK_BROWSER_EXECUTABLE_PATH", "PLAYWRIGHT_BROWSERS_PATH")
            }
            with mock.patch.dict(os.environ, clean_env, clear=True):
                # macOS auto-pins detected chrome-headless-shell
                entry_mac = build_playwright_mcp_entry(
                    state_path=state_file,
                    npx_cmd="/opt/homebrew/bin/npx",
                    platform="darwin",
                    cache_dir=cache,
                    machine="arm64",
                )
                self.assertIn(f"--executable-path={mac_bin.resolve()}", entry_mac["args"])

                # macOS without installed shell omits --executable-path cleanly
                entry_missing = build_playwright_mcp_entry(
                    state_path=state_file,
                    npx_cmd="/opt/homebrew/bin/npx",
                    platform="darwin",
                    cache_dir=empty_cache,
                    machine="arm64",
                )
                self.assertFalse(any(a.startswith("--executable-path") for a in entry_missing["args"]))

                # macOS with SESSFORK_BROWSER_EXECUTABLE_PATH=system disables pinning
                with mock.patch.dict(os.environ, {"SESSFORK_BROWSER_EXECUTABLE_PATH": "system"}):
                    entry_sys = build_playwright_mcp_entry(
                        state_path=state_file,
                        npx_cmd="/opt/homebrew/bin/npx",
                        platform="darwin",
                        cache_dir=cache,
                        machine="arm64",
                    )
                    self.assertFalse(any(a.startswith("--executable-path") for a in entry_sys["args"]))

                # Linux does NOT auto-pin unless SESSFORK_BROWSER_EXECUTABLE_PATH is explicitly set
                entry_linux = build_playwright_mcp_entry(
                    state_path=state_file,
                    npx_cmd="/usr/bin/npx",
                    platform="linux",
                    cache_dir=cache,
                    machine="x86_64",
                )
                self.assertFalse(any(a.startswith("--executable-path") for a in entry_linux["args"]))

                with mock.patch.dict(os.environ, {"SESSFORK_BROWSER_EXECUTABLE_PATH": str(linux_bin)}):
                    entry_linux_override = build_playwright_mcp_entry(
                        state_path=state_file,
                        npx_cmd="/usr/bin/npx",
                        platform="linux",
                        cache_dir=cache,
                        machine="x86_64",
                    )
                    self.assertIn(f"--executable-path={linux_bin.resolve()}", entry_linux_override["args"])

    def test_inspect_mcp_headless_shell_and_cli_status_setup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox_home = Path(tmp) / "home"
            sandbox_home.mkdir()
            state_file = sandbox_home / ".sessfork" / "state.json"
            cache = Path(tmp) / "ms-playwright"
            mac_bin = cache / "chromium_headless_shell-1254" / "chrome-headless-shell-mac-arm64" / "chrome-headless-shell"
            mac_bin.parent.mkdir(parents=True)
            mac_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            os.chmod(mac_bin, 0o755)
            empty_cache = Path(tmp) / "empty-cache"
            empty_cache.mkdir()

            cfg_path = sandbox_home / ".gemini" / "config" / "mcp_config.json"
            cfg_path.parent.mkdir(parents=True)

            # 1. Unpinned wired config on macOS -> warns to run `sessfork --setup`
            cfg_path.write_text(
                json.dumps({"mcpServers": {MCP_SERVER_NAME: {"command": "npx", "args": ["-y", "@playwright/mcp@0.0.85"]}}}),
                encoding="utf-8",
            )
            clean_env = {
                k: v
                for k, v in os.environ.items()
                if k not in ("SESSFORK_BROWSER_EXECUTABLE_PATH", "PLAYWRIGHT_BROWSERS_PATH")
            }
            with mock.patch.dict(os.environ, clean_env, clear=True):
                res_unpinned = inspect_mcp_headless_shell(
                    config_paths=[cfg_path],
                    cache_dir=cache,
                    platform="darwin",
                    machine="arm64",
                )
                self.assertEqual(res_unpinned["playwright_headless_shell"], str(mac_bin.resolve()))
                self.assertEqual(res_unpinned["pinned_mcp_configs"], [])
                self.assertEqual(res_unpinned["unpinned_mcp_configs"], [str(cfg_path)])
                self.assertIn("sessfork --setup", res_unpinned["warning"] or "")

                # 2. Stale --executable-path pointing to missing binary -> warns
                cfg_path.write_text(
                    json.dumps(
                        {
                            "mcpServers": {
                                MCP_SERVER_NAME: {
                                    "command": "npx",
                                    "args": ["-y", "@playwright/mcp@0.0.85", "--executable-path=/nonexistent/shell"],
                                }
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                res_stale = inspect_mcp_headless_shell(
                    config_paths=[cfg_path],
                    cache_dir=cache,
                    platform="darwin",
                    machine="arm64",
                )
                self.assertEqual(res_stale["stale_mcp_configs"], [str(cfg_path)])
                self.assertIn("missing --executable-path", res_stale["warning"] or "")

                # 3. Missing chrome-headless-shell on macOS -> warns with install command
                res_missing = inspect_mcp_headless_shell(
                    config_paths=[],
                    cache_dir=empty_cache,
                    platform="darwin",
                    machine="arm64",
                )
                self.assertIsNone(res_missing["playwright_headless_shell"])
                self.assertIn("playwright-core install chromium-headless-shell", res_missing["warning"] or "")
                self.assertIn("sessfork --setup", res_missing["warning"] or "")

                # 4. Linux without chrome-headless-shell -> no macOS crash warning
                res_linux = inspect_mcp_headless_shell(
                    config_paths=[],
                    cache_dir=empty_cache,
                    platform="linux",
                    machine="x86_64",
                )
                self.assertIsNone(res_linux["warning"])

                # 4b. Explicitly disabled via SESSFORK_BROWSER_EXECUTABLE_PATH=system -> no warning on macOS
                with mock.patch.dict(os.environ, {"SESSFORK_BROWSER_EXECUTABLE_PATH": "system"}):
                    res_disabled = inspect_mcp_headless_shell(
                        config_paths=[],
                        cache_dir=empty_cache,
                        platform="darwin",
                        machine="arm64",
                    )
                    self.assertTrue(res_disabled["auto_pin_disabled"])
                    self.assertIsNone(res_disabled["playwright_headless_shell"])
                    self.assertIsNone(res_disabled["warning"])

                # 4b2. Invalid explicit SESSFORK_BROWSER_EXECUTABLE_PATH warns explicitly on macOS & Linux
                bad_override = str(Path(tmp) / "does-not-exist-browser")
                non_exec_override = Path(tmp) / "non-exec-browser"
                non_exec_override.write_text("#!/bin/sh\n", encoding="utf-8")
                os.chmod(non_exec_override, 0o644)

                with mock.patch.dict(os.environ, {"SESSFORK_BROWSER_EXECUTABLE_PATH": bad_override}):
                    res_bad_mac = inspect_mcp_headless_shell(
                        config_paths=[],
                        cache_dir=cache,
                        platform="darwin",
                        machine="arm64",
                    )
                    self.assertIsNone(res_bad_mac["playwright_headless_shell"])
                    self.assertFalse(res_bad_mac["auto_pin_disabled"])
                    self.assertIn("SESSFORK_BROWSER_EXECUTABLE_PATH", res_bad_mac["warning"] or "")
                    self.assertNotIn("playwright-core install chromium-headless-shell", res_bad_mac["warning"] or "")

                    res_bad_linux = inspect_mcp_headless_shell(
                        config_paths=[],
                        cache_dir=cache,
                        platform="linux",
                        machine="x86_64",
                    )
                    self.assertIsNone(res_bad_linux["playwright_headless_shell"])
                    self.assertIn("SESSFORK_BROWSER_EXECUTABLE_PATH", res_bad_linux["warning"] or "")

                    # Stale MCP config + invalid override keeps stale warning intact AND flags bad env override
                    res_stale_and_bad = inspect_mcp_headless_shell(
                        config_paths=[cfg_path],
                        cache_dir=cache,
                        platform="darwin",
                        machine="arm64",
                    )
                    self.assertEqual(res_stale_and_bad["stale_mcp_configs"], [str(cfg_path)])
                    self.assertIn("missing --executable-path", res_stale_and_bad["warning"] or "")
                    self.assertIn("SESSFORK_BROWSER_EXECUTABLE_PATH", res_stale_and_bad["warning"] or "")

                with mock.patch.dict(os.environ, {"SESSFORK_BROWSER_EXECUTABLE_PATH": str(non_exec_override)}):
                    res_nonexec = inspect_mcp_headless_shell(
                        config_paths=[],
                        cache_dir=cache,
                        platform="linux",
                        machine="x86_64",
                    )
                    self.assertIsNone(res_nonexec["playwright_headless_shell"])
                    self.assertIn("SESSFORK_BROWSER_EXECUTABLE_PATH", res_nonexec["warning"] or "")

                # 4c. Two-token ["--executable-path", "<path>"] is also recognized as pinned
                cfg_path.write_text(
                    json.dumps(
                        {
                            "mcpServers": {
                                MCP_SERVER_NAME: {
                                    "command": "npx",
                                    "args": ["-y", "@playwright/mcp@0.0.85", "--executable-path", str(mac_bin)],
                                }
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                res_two_tok = inspect_mcp_headless_shell(
                    config_paths=[cfg_path],
                    cache_dir=cache,
                    platform="darwin",
                    machine="arm64",
                )
                self.assertEqual(res_two_tok["pinned_mcp_configs"], [str(cfg_path)])
                self.assertEqual(res_two_tok["unpinned_mcp_configs"], [])
                self.assertEqual(res_two_tok["stale_mcp_configs"], [])
                self.assertIsNone(res_two_tok["warning"])

                # 5. CLI --setup and --status on macOS (missing shell -> warning; installed shell -> pinned & clean)
                env_missing = dict(clean_env)
                env_missing["HOME"] = str(sandbox_home)
                env_missing["SESSFORK_HOME"] = str(sandbox_home)
                env_missing["PLAYWRIGHT_BROWSERS_PATH"] = str(empty_cache)
                with mock.patch.dict(os.environ, env_missing, clear=True), \
                    mock.patch("sessfork.mcp_setup.sys.platform", "darwin"), \
                    mock.patch("sessfork.mcp_setup.platform_mod.machine", return_value="arm64"), \
                    mock.patch("sessfork.mcp_setup.resolve_npx_command", return_value="/opt/homebrew/bin/npx"):
                    out_buf = io.StringIO()
                    with contextlib.redirect_stdout(out_buf):
                        rc = main(["--setup", "--target", "antigravity", "--state-file", str(state_file), "--json"])
                    self.assertEqual(rc, 0)
                    setup_payload = json.loads(out_buf.getvalue())
                    self.assertIsNone(setup_payload["playwright_headless_shell"])
                    self.assertIn("playwright-core install chromium-headless-shell", setup_payload["warning"] or "")

                    out_setup_text = io.StringIO()
                    with contextlib.redirect_stdout(out_setup_text):
                        rc = main(["--setup", "--target", "antigravity", "--state-file", str(state_file)])
                    self.assertEqual(rc, 0)
                    self.assertIn("playwright-core install chromium-headless-shell", out_setup_text.getvalue())

                    out_text = io.StringIO()
                    with contextlib.redirect_stdout(out_text):
                        rc = main(["--status", "--target", "antigravity", "--state-file", str(state_file)])
                    self.assertEqual(rc, 0)
                    self.assertIn("Playwright headless shell:", out_text.getvalue())
                    self.assertIn("playwright-core install chromium-headless-shell", out_text.getvalue())

                    # Also verify --status text when state file does not exist yet
                    missing_state = sandbox_home / ".sessfork" / "does-not-exist.json"
                    out_no_state = io.StringIO()
                    with contextlib.redirect_stdout(out_no_state):
                        rc = main(["--status", "--target", "antigravity", "--state-file", str(missing_state)])
                    self.assertEqual(rc, 0)
                    self.assertIn("Playwright headless shell: not found", out_no_state.getvalue())
                    self.assertIn("playwright-core install chromium-headless-shell", out_no_state.getvalue())

                # Now with chrome-headless-shell installed -> --setup pins it, --status reports pinned & no warning
                env_present = dict(clean_env)
                env_present["HOME"] = str(sandbox_home)
                env_present["SESSFORK_HOME"] = str(sandbox_home)
                env_present["PLAYWRIGHT_BROWSERS_PATH"] = str(cache)
                with mock.patch.dict(os.environ, env_present, clear=True), \
                    mock.patch("sessfork.mcp_setup.sys.platform", "darwin"), \
                    mock.patch("sessfork.mcp_setup.platform_mod.machine", return_value="arm64"), \
                    mock.patch("sessfork.mcp_setup.resolve_npx_command", return_value="/opt/homebrew/bin/npx"):
                    out_setup = io.StringIO()
                    with contextlib.redirect_stdout(out_setup):
                        rc = main(["--setup", "--target", "antigravity", "--state-file", str(state_file), "--json"])
                    self.assertEqual(rc, 0)
                    setup_ok = json.loads(out_setup.getvalue())
                    self.assertEqual(setup_ok["playwright_headless_shell"], str(mac_bin.resolve()))
                    self.assertIsNone(setup_ok["warning"])

                    wired_doc = json.loads(cfg_path.read_text(encoding="utf-8"))
                    self.assertIn(
                        f"--executable-path={mac_bin.resolve()}",
                        wired_doc["mcpServers"][MCP_SERVER_NAME]["args"],
                    )

                    out_status = io.StringIO()
                    with contextlib.redirect_stdout(out_status):
                        rc = main(["--status", "--target", "antigravity", "--state-file", str(state_file), "--json"])
                    self.assertEqual(rc, 0)
                    status_payload = json.loads(out_status.getvalue())
                    self.assertEqual(status_payload["playwright_headless_shell"], str(mac_bin.resolve()))
                    self.assertEqual(status_payload["pinned_mcp_configs"], [str(cfg_path)])
                    self.assertEqual(status_payload["unpinned_mcp_configs"], [])
                    self.assertIsNone(status_payload["warning"])


if __name__ == "__main__":
    unittest.main()
