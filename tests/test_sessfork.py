"""End-to-end unit tests for sessfork (zero network, zero OS keychain prompts)."""

from __future__ import annotations

import hashlib
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

from sessfork.chrome import CHROMIUM_EPOCH_DELTA_SECONDS, extract_cookies, normalize_domain_filter
from sessfork.cli import main
from sessfork.crypto import (
    MACOS_ITERATIONS,
    _read_macos_keychain,
    aes_128_cbc_encrypt_pkcs7,
    decrypt_chromium_cookie,
    derive_aes_key,
    get_safe_storage_key,
)
from sessfork.mcp_setup import MCP_SERVER_NAME, ensure_mcp_configured, install_agent_skills
from sessfork.state import clear_cookies, load_storage_state, summarize_state, upsert_cookies


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


if __name__ == "__main__":
    unittest.main()
