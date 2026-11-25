"""Lock-free Chromium SQLite cookie & LevelDB LocalStorage extraction."""

from __future__ import annotations

import ipaddress
import os
import pwd
import re
import shutil
import sqlite3
import stat
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
            "vivaldi": [app_support / "Vivaldi"],
            "chromium": [app_support / "Chromium"],
        }
    else:
        cfg = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
        mapping = {
            "chrome": [cfg / "google-chrome", cfg / "chromium"],
            "canary": [cfg / "google-chrome-unstable"],
            "brave": [cfg / "BraveSoftware" / "Brave-Browser"],
            "edge": [cfg / "microsoft-edge"],
            "arc": [],
            "vivaldi": [cfg / "vivaldi"],
            "chromium": [cfg / "chromium"],
        }
    return mapping.get(browser, [])


def validate_profile_name(profile: str) -> str:
    """Validate that a Chromium profile name is a single safe directory component."""
    cleaned = profile.strip()
    if (
        not cleaned
        or cleaned in (".", "..")
        or "/" in cleaned
        or "\\" in cleaned
        or "\x00" in cleaned
        or Path(cleaned).is_absolute()
        or len(Path(cleaned).parts) != 1
    ):
        raise BrowserProfileError(f"Invalid browser profile name: {profile!r}")
    return cleaned


def _reject_symlink_chain(base: Path, *parts: str) -> Path:
    """Join `parts` under `base` while rejecting any symlink component or escape outside `base`."""
    cur = base
    for part in parts:
        cur = cur / part
        if cur.is_symlink():
            raise BrowserProfileError(f"Refusing to traverse symlinked browser path: {cur}")
    try:
        resolved_base = base.resolve()
        resolved_cur = cur.resolve()
        if not resolved_cur.is_relative_to(resolved_base):
            raise BrowserProfileError(f"Browser path escapes user data directory: {cur}")
    except OSError as exc:
        raise BrowserProfileError(f"Cannot resolve browser path {cur}: {exc}") from exc
    return cur


def _candidate_cookie_files(profile_dir: Path) -> List[Path]:
    # Modern Chromium (96+) stores active cookies under Network/Cookies; check it first
    # so stale pre-upgrade root Cookies files never shadow live sessions.
    return [
        profile_dir / "Network" / "Cookies",
        profile_dir / "Cookies",
    ]


def _is_safe_cookie_candidate(profile_dir: Path, cand: Path) -> bool:
    if profile_dir.is_symlink():
        return False
    if cand.parent != profile_dir and cand.parent.is_symlink():
        return False
    return not cand.is_symlink() and cand.is_file()


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
        raw_expanded = db_path.expanduser()
        if raw_expanded.is_symlink() or raw_expanded.parent.is_symlink():
            raise BrowserProfileError(f"Refusing to read symlinked Cookie DB: {raw_expanded}")
        expanded = raw_expanded.resolve()
        if not expanded.is_file():
            raise BrowserProfileError(f"Cookie DB file not found: {expanded}")
        return expanded

    base_dirs = [d for d in _browser_base_dirs(browser) if d.is_dir() and not d.is_symlink()]
    if not base_dirs:
        raise BrowserProfileError(
            f"Could not find user data directory for browser '{browser}'."
        )

    if profile is not None:
        safe_profile = validate_profile_name(profile)
        for base in base_dirs:
            pdir = _reject_symlink_chain(base, safe_profile)
            for cand in _candidate_cookie_files(pdir):
                if cand.parent != pdir and cand.parent.is_symlink():
                    raise BrowserProfileError(f"Refusing to traverse symlinked Cookie directory: {cand.parent}")
                if cand.is_symlink():
                    raise BrowserProfileError(f"Refusing to read symlinked Cookie DB: {cand}")
                if cand.is_file():
                    return cand
        raise BrowserProfileError(
            f"Profile '{safe_profile}' Cookies database not found under {base_dirs[0]}"
        )

    found: List[Path] = []
    for base in base_dirs:
        profile_dirs = [base / "Default"] + sorted(base.glob("Profile *"))
        for pdir in profile_dirs:
            if pdir.is_symlink() or not pdir.is_dir():
                continue
            for cand in _candidate_cookie_files(pdir):
                if _is_safe_cookie_candidate(pdir, cand):
                    found.append(cand)
                    break

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
        raw_expanded = leveldb_path.expanduser()
        if raw_expanded.is_symlink() or raw_expanded.parent.is_symlink():
            raise BrowserProfileError(
                f"Refusing to read symlinked LocalStorage LevelDB directory: {raw_expanded}"
            )
        expanded = raw_expanded.resolve()
        if not expanded.is_dir():
            raise BrowserProfileError(f"LocalStorage LevelDB directory not found: {expanded}")
        return expanded

    if cookie_db is not None:
        prof_dir = profile_dir_from_cookie_db(cookie_db)
        ls_parent = prof_dir / "Local Storage"
        candidate = ls_parent / "leveldb"
        if ls_parent.is_symlink() or candidate.is_symlink():
            raise BrowserProfileError(
                f"Refusing to read symlinked LocalStorage LevelDB directory: {candidate}"
            )
        return candidate if candidate.is_dir() else None

    base_dirs = [d for d in _browser_base_dirs(browser) if d.is_dir() and not d.is_symlink()]
    if not base_dirs:
        return None

    if profile is not None:
        safe_profile = validate_profile_name(profile)
        for base in base_dirs:
            candidate = _reject_symlink_chain(base, safe_profile, "Local Storage", "leveldb")
            if candidate.is_dir():
                return candidate
        return None

    candidates: List[Path] = []
    for base in base_dirs:
        for pdir in [base / "Default"] + sorted(base.glob("Profile *")):
            if pdir.is_symlink() or not pdir.is_dir():
                continue
            ls_parent = pdir / "Local Storage"
            candidate = ls_parent / "leveldb"
            if ls_parent.is_symlink() or candidate.is_symlink():
                continue
            if candidate.is_dir():
                candidates.append(candidate)

    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


_VALID_HOSTNAME_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)
_GENERIC_SECOND_LEVEL_CCTLD_RE = re.compile(
    r"^(?:co|com|net|org|gov|edu|ac|go|or|ne|mil|sch|ltd|plc|me)\.[a-z]{2}$"
)
_UNSAFE_PUBLIC_SUFFIXES = frozenset(
    {
        # Multi-label country/registry eTLDs
        "co.uk", "org.uk", "ac.uk", "gov.uk",
        "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn",
        "com.au", "net.au", "org.au", "edu.au", "gov.au",
        "co.jp", "ne.jp", "or.jp", "ac.jp", "go.jp",
        "co.kr", "com.br", "com.hk", "com.tw", "com.sg", "co.in",
        "co.nz", "co.za", "co.id", "co.il", "com.mx", "com.ar",
        "com.my", "com.tr", "com.vn",
        # Multi-tenant shared hosting & cloud public suffixes
        "github.io", "gitlab.io", "vercel.app", "netlify.app",
        "pages.dev", "workers.dev", "web.app", "firebaseapp.com",
        "herokuapp.com", "onrender.com", "fly.dev", "railway.app",
        "appspot.com", "run.app", "azurewebsites.net", "cloudfront.net",
        "amazonaws.com", "s3.amazonaws.com", "googleusercontent.com",
        "myshopify.com", "notion.site", "hf.space", "streamlit.app",
        "supabase.co", "ngrok.io", "ngrok-free.app", "trycloudflare.com",
        "deno.dev", "replit.app", "replit.dev", "glitch.me",
        "surge.sh", "bitbucket.io", "azurestaticapps.net",
    }
)


def normalize_domain_filter(raw: str) -> str:
    """Normalize and validate a URL or domain input (e.g., 'https://gist.github.com/foo' -> 'gist.github.com')."""
    text = raw.strip()
    if not text:
        raise BrowserProfileError(
            "Domain filter cannot be empty; specify a concrete domain (e.g. 'github.com') "
            "or pass --all-domains explicitly."
        )
    if "://" in text:
        parsed = urlparse(text)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise BrowserProfileError(f"Invalid URL or domain filter: {raw!r}")
        host = parsed.hostname.lstrip(".").lower()
    else:
        if any(ch in text for ch in " \t\r\n;|&$`'\"\\<>"):
            raise BrowserProfileError(f"Invalid characters in domain filter: {raw!r}")
        host = text.split("/")[0].split(":")[0].lstrip(".").lower()

    if host == "localhost":
        return host
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass

    if (
        not _VALID_HOSTNAME_RE.match(host)
        or host in _UNSAFE_PUBLIC_SUFFIXES
        or _GENERIC_SECOND_LEVEL_CCTLD_RE.match(host)
    ):
        raise BrowserProfileError(
            f"Refusing unsafe or invalid domain filter {raw!r}; "
            "specify a concrete domain (e.g. 'github.com' or 'user.github.io')."
        )
    return host


def _domain_matches(
    host_key: str,
    filters: Optional[Sequence[str]],
    *,
    all_domains: bool = False,
) -> bool:
    if all_domains or filters is None:
        return True
    if not filters:
        return False
    clean_host = host_key.lstrip(".").lower()
    for dom in filters:
        if clean_host == dom or clean_host.endswith("." + dom):
            return True
    return False


def _convert_expires(expires_utc: int, has_expires: int) -> float:
    if not has_expires or expires_utc <= 0:
        return -1
    unix_sec = (expires_utc / 1_000_000.0) - CHROMIUM_EPOCH_DELTA_SECONDS
    return round(unix_sec, 3) if unix_sec > 0 else 0.0


def _convert_samesite(raw_samesite: int, is_secure: bool) -> str:
    if raw_samesite == 2:
        return "Strict"
    if raw_samesite == 0:
        return "None" if is_secure else "Lax"
    return "Lax"


def _copy_file_0600(src: Path, dst: Path) -> None:
    read_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    src_fd = os.open(str(src), read_flags)
    src_owned = True
    try:
        src_st = os.fstat(src_fd)
        if not stat.S_ISREG(src_st.st_mode):
            raise BrowserProfileError(f"Refusing to copy non-regular browser file: {src}")
        write_flags = os.O_CREAT | os.O_TRUNC | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        dst_fd = os.open(str(dst), write_flags, 0o600)
        dst_owned = True
        try:
            os.fchmod(dst_fd, 0o600)
            src_owned = False
            dst_owned = False
            with os.fdopen(src_fd, "rb") as rfh, os.fdopen(dst_fd, "wb") as wfh:
                shutil.copyfileobj(rfh, wfh)
        except BaseException:
            if dst_owned:
                try:
                    os.close(dst_fd)
                except OSError:
                    pass
            try:
                dst.unlink()
            except OSError:
                pass
            raise
    finally:
        if src_owned:
            try:
                os.close(src_fd)
            except OSError:
                pass


def _snapshot_cookie_db(source_db: Path, tmp_db: Path, timeout_seconds: float = 2.0) -> None:
    """Create a consistent 0600 SQLite snapshot using sqlite3.backup(), with 0600 file fallback.

    Probes read permissions with `O_NOFOLLOW` first so macOS TCC PermissionError is preserved
    and symlinked DBs or WAL/SHM/journal sidecars are rejected before SQLite opens them.
    """
    if source_db.is_symlink():
        raise BrowserProfileError(f"Refusing to read symlinked Cookie DB: {source_db}")
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(source_db) + suffix)
        if sidecar.is_symlink():
            raise BrowserProfileError(f"Refusing to read symlinked Cookie DB sidecar: {sidecar}")

    probe_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    probe_fd = os.open(str(source_db), probe_flags)
    try:
        if not stat.S_ISREG(os.fstat(probe_fd).st_mode):
            raise BrowserProfileError(f"Refusing non-regular Cookie DB file: {source_db}")
    finally:
        os.close(probe_fd)

    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(tmp_db), flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)

    deadline = time.monotonic() + max(0.2, timeout_seconds)

    def _check_deadline(_status: int, _remaining: int, _total: int) -> None:
        if time.monotonic() > deadline:
            raise TimeoutError("SQLite snapshot backup exceeded deadline")

    reader: Optional[sqlite3.Connection] = None
    writer: Optional[sqlite3.Connection] = None
    try:
        reader = sqlite3.connect(f"{source_db.as_uri()}?mode=ro", uri=True, timeout=0)
        writer = sqlite3.connect(str(tmp_db))
        reader.backup(writer, pages=128, progress=_check_deadline, sleep=0.01)
        writer.commit()
        return
    except (sqlite3.Error, TimeoutError):
        # Fallback for read-only directories or exotic filesystems where WAL SHM cannot be opened.
        pass
    finally:
        if writer is not None:
            writer.close()
        if reader is not None:
            reader.close()

    _copy_file_0600(source_db, tmp_db)
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(source_db) + suffix)
        if sidecar.is_symlink():
            raise BrowserProfileError(f"Refusing to read symlinked Cookie DB sidecar: {sidecar}")
        if sidecar.is_file():
            _copy_file_0600(sidecar, Path(str(tmp_db) + suffix))


def _cookie_priority_key(
    is_partitioned: bool,
    is_secure: int,
    is_httponly: int,
    expires: float,
) -> tuple[int, int, int, float]:
    return (
        0 if is_partitioned else 1,
        1 if is_secure else 0,
        1 if is_httponly else 0,
        float("inf") if expires == -1 else expires,
    )


def extract_cookies(
    domains: Iterable[str],
    browser: str = "chrome",
    profile: Optional[str] = None,
    db_path: Optional[Path] = None,
    aes_key: Optional[bytes] = None,
    include_expired: bool = False,
    refresh_key: bool = False,
    cache_key: bool = False,
    include_partitioned: bool = False,
    all_domains: bool = False,
) -> tuple[List[CookieDict], Path]:
    """Snapshot Chromium SQLite DB to an isolated 0700 temp dir and extract Playwright cookies."""
    raw_domains = list(domains)
    if all_domains:
        normalized_domains: Optional[List[str]] = None
    else:
        if not raw_domains:
            raise BrowserProfileError(
                "At least one domain is required (or pass all_domains=True / --all-domains)."
            )
        normalized_domains = [normalize_domain_filter(d) for d in raw_domains]

    source_db = resolve_cookie_db(browser=browser, profile=profile, db_path=db_path)
    if source_db.is_symlink():
        raise BrowserProfileError(f"Refusing to read symlinked Cookie DB: {source_db}")
    now = time.time()

    with tempfile.TemporaryDirectory(prefix="sessfork_") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        os.chmod(tmp_dir, 0o700)
        tmp_db = tmp_dir / "Cookies"
        _snapshot_cookie_db(source_db, tmp_db)

        try:
            conn = sqlite3.connect(str(tmp_db))
            try:
                cur = conn.cursor()
                db_version = _read_meta_version(cur)
                cur.execute("PRAGMA table_info(cookies)")
                columns = {str(row[1]) for row in cur.fetchall() if len(row) > 1}
                top_frame_col = "top_frame_site_key" if "top_frame_site_key" in columns else "''"
                cur.execute(
                    f"""
                    SELECT host_key, name, value, encrypted_value, path,
                           expires_utc, is_secure, is_httponly, has_expires, samesite,
                           {top_frame_col}
                    FROM cookies
                    """
                )
                raw_rows = cur.fetchall()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise BrowserProfileError(f"Failed to read Chromium Cookies SQLite database {source_db}: {exc}") from exc

    # Deduplicate by (host_key, path, name) with deterministic priority so third-party
    # CHIPS partitioned rows never overwrite primary unpartitioned session cookies.
    best_by_key: Dict[tuple[str, str, str], tuple[tuple[int, int, int, float], tuple[str, str, str, bytes, str, float, int, int, int]]] = {}
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
        top_frame_site_key,
    ) in raw_rows:
        if not _domain_matches(host_key, normalized_domains, all_domains=all_domains):
            continue

        # Chromium stores has_cross_site_ancestor=1 on unpartitioned cookies too; only
        # a non-empty top_frame_site_key indicates a CHIPS-partitioned cookie.
        is_partitioned = bool(str(top_frame_site_key or "").strip())
        if is_partitioned and not include_partitioned:
            continue

        expires = _convert_expires(int(expires_utc or 0), int(has_expires or 0))
        if not include_expired and expires != -1 and expires <= now:
            continue

        norm_path = path or "/"
        sec_int = int(is_secure or 0)
        http_int = int(is_httponly or 0)
        prio = _cookie_priority_key(is_partitioned, sec_int, http_int, expires)
        row_tuple = (
            host_key,
            name,
            plain_val or "",
            bytes(enc_val) if enc_val else b"",
            norm_path,
            expires,
            sec_int,
            http_int,
            int(samesite) if samesite is not None else -1,
        )
        ident = (host_key, norm_path, name)
        prev = best_by_key.get(ident)
        if prev is None or prio > prev[0]:
            best_by_key[ident] = (prio, row_tuple)

    matched_rows = [entry[1] for entry in best_by_key.values()]

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
    all_domains: bool = False,
) -> tuple[List[CookieDict], List[OriginEntry], str]:
    """Extract both SQLite Cookies and LevelDB LocalStorage for `domains` without locking the browser.

    Returns `(cookies, origins, profile_label)`.
    """
    domain_list = list(domains)
    if all_domains:
        normalized_domains: Optional[List[str]] = None
    else:
        if not domain_list:
            raise BrowserProfileError(
                "At least one domain is required (or pass all_domains=True / --all-domains)."
            )
        normalized_domains = [normalize_domain_filter(d) for d in domain_list]

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
            all_domains=all_domains,
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
            origins = extract_local_storage(ls_dir, normalized_domains, all_domains=all_domains)
            if not include_cookies:
                profile_label = ls_dir.parent.parent.name

    return cookies, origins, profile_label


def _read_meta_version(cur: sqlite3.Cursor) -> Optional[int]:
    try:
        cur.execute("SELECT value FROM meta WHERE key = 'version'")
        row = cur.fetchone()
        if row and row[0] is not None:
            return int(str(row[0]))
    except (sqlite3.Error, ValueError):
        pass
    return None
