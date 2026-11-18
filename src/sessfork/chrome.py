"""Lock-free Chromium SQLite cookie & LevelDB LocalStorage extraction."""

from __future__ import annotations

import os
import pwd
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import urlparse

from sessfork.crypto import decrypt_chromium_cookie, get_safe_storage_key
from sessfork.localstorage import OriginEntry, extract_local_storage

# Microseconds between 1601-01-01 UTC (Windows FILETIME) and 1970-01-01 UTC (Unix epoch)
CHROMIUM_EPOCH_DELTA_SECONDS = 11644473600

CookieDict = Dict[str, Any]


def real_user_home() -> Path:
    """Resolve the real OS user home directory even inside sandboxed AI agent shells."""
    override = os.environ.get("SESSFORK_HOME")
    if override:
        return Path(override).expanduser()
    try:
        pw_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        if pw_home.is_dir():
            return pw_home
    except KeyError:
        pass
    return Path.home()


class BrowserProfileError(RuntimeError):
    """Raised when a browser profile database cannot be located or read."""


def _browser_base_dirs(browser: str) -> List[Path]:
    home = real_user_home()
    if sys.platform == "darwin":
        app_support = home / "Library" / "Application Support"
        mapping = {
            "chrome": [app_support / "Google" / "Chrome"],
            "canary": [app_support / "Google" / "Chrome Canary"],
            "brave": [app_support / "BraveSoftware" / "Brave-Browser"],
            "edge": [app_support / "Microsoft Edge"],
            "arc": [app_support / "Arc" / "User Data"],
        }
    else:
        cfg = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
        mapping = {
            "chrome": [cfg / "google-chrome", cfg / "chromium"],
            "canary": [cfg / "google-chrome-unstable"],
            "brave": [cfg / "BraveSoftware" / "Brave-Browser"],
            "edge": [cfg / "microsoft-edge"],
            "arc": [],
        }
    return mapping.get(browser, [])


def _candidate_cookie_files(profile_dir: Path) -> List[Path]:
    return [
        profile_dir / "Cookies",
        profile_dir / "Network" / "Cookies",
    ]


def profile_dir_from_cookie_db(cookie_db: Path) -> Path:
    """Infer the Chromium profile root directory from a `Cookies` or `Network/Cookies` path."""
    resolved = cookie_db.expanduser().resolve()
    if resolved.parent.name == "Network":
        return resolved.parent.parent
    return resolved.parent


def resolve_cookie_db(
    browser: str = "chrome",
    profile: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> Path:
    """Locate the active Chromium Cookies SQLite database without locking it."""
    if db_path is not None:
        expanded = db_path.expanduser().resolve()
        if not expanded.is_file():
            raise BrowserProfileError(f"Cookie DB file not found: {expanded}")
        return expanded

    base_dirs = [d for d in _browser_base_dirs(browser) if d.is_dir()]
    if not base_dirs:
        raise BrowserProfileError(
            f"Could not find user data directory for browser '{browser}'."
        )

    if profile:
        for base in base_dirs:
            for cand in _candidate_cookie_files(base / profile):
                if cand.is_file():
                    return cand
        raise BrowserProfileError(
            f"Profile '{profile}' Cookies database not found under {base_dirs[0]}"
        )

    found: List[Path] = []
    for base in base_dirs:
        profile_dirs = [base / "Default"] + sorted(base.glob("Profile *"))
        for pdir in profile_dirs:
            if not pdir.is_dir():
                continue
            for cand in _candidate_cookie_files(pdir):
                if cand.is_file():
                    found.append(cand)

    if not found:
        raise BrowserProfileError(
            f"No Cookies SQLite database found under {base_dirs[0]}"
        )

    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return found[0]


def resolve_localstorage_dir(
    browser: str = "chrome",
    profile: Optional[str] = None,
    leveldb_path: Optional[Path] = None,
    cookie_db: Optional[Path] = None,
) -> Optional[Path]:
    """Locate `<Profile>/Local Storage/leveldb` matching the active or specified profile."""
    if leveldb_path is not None:
        expanded = leveldb_path.expanduser().resolve()
        if not expanded.is_dir():
            raise BrowserProfileError(f"LocalStorage LevelDB directory not found: {expanded}")
        return expanded

    if cookie_db is not None:
        prof_dir = profile_dir_from_cookie_db(cookie_db)
        candidate = prof_dir / "Local Storage" / "leveldb"
        return candidate if candidate.is_dir() else None

    base_dirs = [d for d in _browser_base_dirs(browser) if d.is_dir()]
    if not base_dirs:
        return None

    if profile:
        for base in base_dirs:
            candidate = base / profile / "Local Storage" / "leveldb"
            if candidate.is_dir():
                return candidate
        return None

    candidates: List[Path] = []
    for base in base_dirs:
        for pdir in [base / "Default"] + sorted(base.glob("Profile *")):
            candidate = pdir / "Local Storage" / "leveldb"
            if candidate.is_dir():
                candidates.append(candidate)

    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def normalize_domain_filter(raw: str) -> str:
    """Normalize URL or domain input (e.g., 'https://gist.github.com/foo' -> 'gist.github.com')."""
    text = raw.strip()
    if "://" in text:
        parsed = urlparse(text)
        host = parsed.hostname or text
    else:
        host = text.split("/")[0].split(":")[0]
    return host.lstrip(".").lower()


def _domain_matches(host_key: str, filters: Sequence[str]) -> bool:
    if not filters:
        return True
    clean_host = host_key.lstrip(".").lower()
    for dom in filters:
        if clean_host == dom or clean_host.endswith("." + dom):
            return True
    return False


def _convert_expires(expires_utc: int, has_expires: int) -> float:
    if not has_expires or expires_utc <= 0:
        return -1
    unix_sec = (expires_utc / 1_000_000.0) - CHROMIUM_EPOCH_DELTA_SECONDS
    return round(unix_sec, 3) if unix_sec > 0 else -1


def _convert_samesite(raw_samesite: int, is_secure: bool) -> str:
    if raw_samesite == 2:
        return "Strict"
    if raw_samesite == 0:
        return "None" if is_secure else "Lax"
    return "Lax"


def extract_cookies(
    domains: Iterable[str],
    browser: str = "chrome",
    profile: Optional[str] = None,
    db_path: Optional[Path] = None,
    aes_key: Optional[bytes] = None,
    include_expired: bool = False,
    refresh_key: bool = False,
    cache_key: bool = False,
) -> tuple[List[CookieDict], Path]:
    """Copy Chromium SQLite DB (+ WAL/SHM) to an isolated 0700 temp dir and extract Playwright cookies."""
    source_db = resolve_cookie_db(browser=browser, profile=profile, db_path=db_path)
    normalized_domains = [normalize_domain_filter(d) for d in domains if d.strip()]
    now = time.time()

    with tempfile.TemporaryDirectory(prefix="sessfork_") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        os.chmod(tmp_dir, 0o700)
        tmp_db = tmp_dir / "Cookies"
        shutil.copy2(source_db, tmp_db)

        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(source_db) + suffix)
            if sidecar.is_file():
                shutil.copy2(sidecar, Path(str(tmp_db) + suffix))

        conn = sqlite3.connect(str(tmp_db))
        try:
            cur = conn.cursor()
            db_version = _read_meta_version(cur)
            cur.execute(
                """
                SELECT host_key, name, value, encrypted_value, path,
                       expires_utc, is_secure, is_httponly, has_expires, samesite
                FROM cookies
                """
            )
            raw_rows = cur.fetchall()
        finally:
            conn.close()

    matched_rows: List[tuple[str, str, str, bytes, str, float, int, int, int]] = []
    for (
        host_key,
        name,
        plain_val,
        enc_val,
        path,
        expires_utc,
        is_secure,
        is_httponly,
        has_expires,
        samesite,
    ) in raw_rows:
        if not _domain_matches(host_key, normalized_domains):
            continue

        expires = _convert_expires(int(expires_utc or 0), int(has_expires or 0))
        if not include_expired and expires != -1 and expires <= now:
            continue

        matched_rows.append(
            (
                host_key,
                name,
                plain_val or "",
                bytes(enc_val) if enc_val else b"",
                path or "/",
                expires,
                int(is_secure or 0),
                int(is_httponly or 0),
                int(samesite) if samesite is not None else -1,
            )
        )

    def _decrypt_rows(active_key: bytes) -> tuple[List[CookieDict], int, int]:
        cookies_out: List[CookieDict] = []
        encrypted_seen = 0
        encrypted_ok = 0
        for (
            host_key,
            name,
            plain_val,
            enc_bytes,
            path,
            expires,
            is_secure,
            is_httponly,
            samesite,
        ) in matched_rows:
            is_aes_row = (not plain_val) and enc_bytes[:3] in (b"v10", b"v11")
            if is_aes_row:
                encrypted_seen += 1
            decrypted = decrypt_chromium_cookie(
                encrypted_value=enc_bytes,
                plain_value=plain_val,
                host_key=host_key,
                aes_key=active_key,
                db_version=db_version,
            )
            if decrypted is None:
                continue
            if is_aes_row:
                encrypted_ok += 1

            secure_bool = bool(is_secure)
            cookies_out.append(
                {
                    "name": name,
                    "value": decrypted,
                    "domain": host_key,
                    "path": path,
                    "expires": expires,
                    "httpOnly": bool(is_httponly),
                    "secure": secure_bool,
                    "sameSite": _convert_samesite(samesite, secure_bool),
                }
            )
        return cookies_out, encrypted_seen, encrypted_ok

    needs_aes = any(
        (not plain_val) and enc_bytes[:3] in (b"v10", b"v11")
        for (_, _, plain_val, enc_bytes, *_) in matched_rows
    )
    if not needs_aes:
        cookies, _, _ = _decrypt_rows(b"")
        return cookies, source_db

    key = (
        aes_key
        if aes_key is not None
        else get_safe_storage_key(browser=browser, use_cache=cache_key, refresh=refresh_key)
    )
    cookies, encrypted_seen, encrypted_ok = _decrypt_rows(key)
    # Self-heal a stale cached key (only meaningful when the opt-in cache was used)
    if aes_key is None and cache_key and not refresh_key and encrypted_seen > 0 and encrypted_ok == 0:
        key = get_safe_storage_key(browser=browser, use_cache=True, refresh=True)
        cookies, _, _ = _decrypt_rows(key)

    return cookies, source_db


def extract_browser_session(
    domains: Iterable[str],
    browser: str = "chrome",
    profile: Optional[str] = None,
    db_path: Optional[Path] = None,
    leveldb_path: Optional[Path] = None,
    aes_key: Optional[bytes] = None,
    include_cookies: bool = True,
    include_local_storage: bool = True,
    refresh_key: bool = False,
    cache_key: bool = False,
) -> tuple[List[CookieDict], List[OriginEntry], str]:
    """Extract both SQLite Cookies and LevelDB LocalStorage for `domains` without locking the browser.

    Returns `(cookies, origins, profile_label)`.
    """
    domain_list = list(domains)
    normalized_domains = [normalize_domain_filter(d) for d in domain_list if d.strip()]

    cookies: List[CookieDict] = []
    source_cookie_db: Optional[Path] = None
    profile_label = profile or "Default"

    if include_cookies:
        cookies, source_cookie_db = extract_cookies(
            domains=domain_list,
            browser=browser,
            profile=profile,
            db_path=db_path,
            aes_key=aes_key,
            refresh_key=refresh_key,
            cache_key=cache_key,
        )
        profile_label = profile_dir_from_cookie_db(source_cookie_db).name

    origins: List[OriginEntry] = []
    if include_local_storage:
        ls_dir = resolve_localstorage_dir(
            browser=browser,
            profile=profile,
            leveldb_path=leveldb_path,
            cookie_db=source_cookie_db,
        )
        if ls_dir is not None:
            origins = extract_local_storage(ls_dir, normalized_domains)
            if not include_cookies:
                profile_label = ls_dir.parent.parent.name

    return cookies, origins, profile_label


def _read_meta_version(cur: sqlite3.Cursor) -> int:
    try:
        cur.execute("SELECT value FROM meta WHERE key = 'version'")
        row = cur.fetchone()
        if row and row[0] is not None:
            return int(str(row[0]))
    except (sqlite3.Error, ValueError):
        pass
    return 24
