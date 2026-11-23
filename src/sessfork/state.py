"""Playwright storageState file management (Cookies + LocalStorage origins) with 0600 permissions."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from sessfork.chrome import (
    BrowserProfileError,
    CookieDict,
    normalize_domain_filter,
    real_user_home,
)
from sessfork.localstorage import OriginEntry

DEFAULT_STATE_PATH = real_user_home() / ".sessfork" / "state.json"
PLAYWRIGHT_OUTPUT_DIRNAME = "playwright-output"
HOT_RELOAD_STATE_FILENAME = ".sessfork-state.json"


def hot_reload_state_path(state_path: Path = DEFAULT_STATE_PATH) -> Path:
    """Return mirror path inside Playwright MCP `--output-dir` for zero-restart hot reload."""
    resolved = state_path.expanduser().resolve()
    return resolved.parent / PLAYWRIGHT_OUTPUT_DIRNAME / HOT_RELOAD_STATE_FILENAME


def _cookie_identity(cookie: CookieDict) -> Tuple[str, str, str]:
    return (
        str(cookie.get("domain", "")),
        str(cookie.get("path", "/")),
        str(cookie.get("name", "")),
    )


def _origin_matches_filters(origin_url: str, filters: Sequence[str]) -> bool:
    host = (urlparse(origin_url).hostname or origin_url).lstrip(".").lower()
    return any(host == f or host.endswith("." + f) for f in filters)


def load_storage_state(state_path: Path = DEFAULT_STATE_PATH) -> Dict[str, Any]:
    """Load an existing Playwright storageState JSON or return an empty skeleton."""
    path = state_path.expanduser()
    if not path.is_file():
        return {"cookies": [], "origins": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            cookies = data.get("cookies")
            origins = data.get("origins")
            return {
                "cookies": cookies if isinstance(cookies, list) else [],
                "origins": origins if isinstance(origins, list) else [],
            }
    except (OSError, json.JSONDecodeError):
        pass
    return {"cookies": [], "origins": []}


def _write_json_0600(payload: str, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)

    fd, tmp_name = tempfile.mkstemp(prefix=".state_", suffix=".tmp", dir=str(target.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp_name, target)
    except Exception:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise
    return target


def _state_lock_path(target: Path) -> Path:
    return target.parent / f".{target.name}.lock"


@contextlib.contextmanager
def _exclusive_state_lock(state_path: Path) -> Iterator[None]:
    """Serialize read-modify-write cycles across concurrent processes and threads."""
    target = state_path.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)
    lock_path = _state_lock_path(target)
    if lock_path.is_symlink():
        raise BrowserProfileError(f"Refusing to use symlinked state lock file: {lock_path}")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(lock_path), flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _save_storage_state_unlocked(state: Dict[str, Any], state_path: Path = DEFAULT_STATE_PATH) -> Path:
    target = state_path.expanduser().resolve()
    payload = json.dumps(state, indent=2, ensure_ascii=False) + "\n"
    saved = _write_json_0600(payload, target)
    mirror = hot_reload_state_path(saved)
    if mirror != saved:
        _write_json_0600(payload, mirror)
    return saved


def save_storage_state_atomic(state: Dict[str, Any], state_path: Path = DEFAULT_STATE_PATH) -> Path:
    """Atomically write storageState JSON (and its Playwright --output-dir mirror) with 0600 mode."""
    with _exclusive_state_lock(state_path):
        return _save_storage_state_unlocked(state, state_path)


def upsert_session_state(
    new_cookies: Iterable[CookieDict],
    new_origins: Optional[Iterable[OriginEntry]] = None,
    refreshed_domains: Optional[Iterable[str]] = None,
    state_path: Path = DEFAULT_STATE_PATH,
    replace_all: bool = False,
) -> tuple[Path, int, int, int, int]:
    """Merge cookies and LocalStorage origins into Playwright storageState.

    For `refreshed_domains`, any previously saved origin matching those domains whose
    LocalStorage was completely cleared in Chrome is pruned so removed JWTs never linger.

    Returns `(saved_path, upserted_cookies, total_cookies, upserted_ls_keys, total_origins)`.
    """
    new_cookies_list = list(new_cookies)
    new_origins_list = None if new_origins is None else list(new_origins)
    norm_refreshed = [normalize_domain_filter(d) for d in (refreshed_domains or []) if d.strip()]

    with _exclusive_state_lock(state_path):
        now = time.time()
        existing = {"cookies": [], "origins": []} if replace_all else load_storage_state(state_path)

        # 1. Merge & prune expired cookies
        merged_cookies: Dict[Tuple[str, str, str], CookieDict] = {}
        for item in existing["cookies"]:
            exp = item.get("expires", -1)
            if isinstance(exp, (int, float)) and exp != -1 and exp <= now:
                continue
            merged_cookies[_cookie_identity(item)] = item

        upserted_cookies = 0
        for item in new_cookies_list:
            merged_cookies[_cookie_identity(item)] = item
            upserted_cookies += 1

        sorted_cookies = sorted(
            merged_cookies.values(),
            key=lambda c: (str(c.get("domain", "")), str(c.get("name", ""))),
        )

        # 2. Merge LocalStorage origins
        merged_origins: Dict[str, List[Dict[str, str]]] = {}

        for entry in existing.get("origins", []):
            if not isinstance(entry, dict):
                continue
            origin_url = str(entry.get("origin", "")).strip()
            ls_items = entry.get("localStorage")
            if not origin_url or not isinstance(ls_items, list):
                continue
            # If we are refreshing this domain's LocalStorage, drop old snapshot first
            # so keys removed via localStorage.removeItem() in Chrome are also removed here.
            if new_origins_list is not None and norm_refreshed and _origin_matches_filters(origin_url, norm_refreshed):
                continue
            merged_origins[origin_url] = ls_items

        upserted_ls_keys = 0
        if new_origins_list is not None:
            for entry in new_origins_list:
                origin_url = str(entry.get("origin", "")).strip()
                ls_items = entry.get("localStorage", [])
                if not origin_url or not isinstance(ls_items, list):
                    continue
                if ls_items:
                    merged_origins[origin_url] = ls_items
                    upserted_ls_keys += len(ls_items)
                else:
                    merged_origins.pop(origin_url, None)

        sorted_origins: List[OriginEntry] = [
            {"origin": orig, "localStorage": merged_origins[orig]}
            for orig in sorted(merged_origins.keys())
            if merged_origins[orig]
        ]

        out_state = {
            "cookies": sorted_cookies,
            "origins": sorted_origins,
        }
        saved = _save_storage_state_unlocked(out_state, state_path)
        return saved, upserted_cookies, len(sorted_cookies), upserted_ls_keys, len(sorted_origins)


def upsert_cookies(
    new_cookies: Iterable[CookieDict],
    state_path: Path = DEFAULT_STATE_PATH,
    replace_all: bool = False,
) -> tuple[Path, int, int]:
    """Backward-compatible helper merging cookies while preserving existing origins."""
    saved, up_c, tot_c, _, _ = upsert_session_state(
        new_cookies=new_cookies,
        new_origins=None,
        state_path=state_path,
        replace_all=replace_all,
    )
    return saved, up_c, tot_c


def clear_state(
    domains: Optional[Iterable[str]] = None,
    state_path: Path = DEFAULT_STATE_PATH,
) -> tuple[int, int, int, int]:
    """Remove cookies AND LocalStorage origins matching `domains` (or all if empty).

    Returns `(removed_cookies, remaining_cookies, removed_origins, remaining_origins)`.
    """
    filters = [normalize_domain_filter(d) for d in (domains or []) if d.strip()]
    with _exclusive_state_lock(state_path):
        state = load_storage_state(state_path)
        before_cookies = len(state["cookies"])
        before_origins = len(state["origins"])

        if not filters:
            state["cookies"] = []
            state["origins"] = []
        else:
            state["cookies"] = [
                item
                for item in state["cookies"]
                if not _origin_matches_filters(str(item.get("domain", "")), filters)
            ]
            state["origins"] = [
                entry
                for entry in state["origins"]
                if not _origin_matches_filters(str(entry.get("origin", "")), filters)
            ]

        _save_storage_state_unlocked(state, state_path)
        rem_cookies = len(state["cookies"])
        rem_origins = len(state["origins"])
        return (
            before_cookies - rem_cookies,
            rem_cookies,
            before_origins - rem_origins,
            rem_origins,
        )


def clear_cookies(
    domains: Optional[Iterable[str]] = None,
    state_path: Path = DEFAULT_STATE_PATH,
) -> tuple[int, int]:
    """Backward-compatible wrapper returning `(removed_cookies, remaining_cookies)`."""
    rem_c, left_c, _, _ = clear_state(domains=domains, state_path=state_path)
    return rem_c, left_c


def list_state_domains(state_path: Path = DEFAULT_STATE_PATH) -> List[str]:
    """Return deduplicated top-level domains currently stored in `state.json`."""
    state = load_storage_state(state_path)
    candidates: set[str] = set()

    for item in state.get("cookies", []):
        if not isinstance(item, dict):
            continue
        raw_dom = str(item.get("domain", "")).strip()
        if not raw_dom:
            continue
        try:
            norm = normalize_domain_filter(raw_dom)
        except BrowserProfileError:
            continue
        if norm:
            candidates.add(norm)

    for entry in state.get("origins", []):
        if not isinstance(entry, dict):
            continue
        raw_orig = str(entry.get("origin", "")).strip()
        if not raw_orig:
            continue
        try:
            norm = normalize_domain_filter(raw_orig)
        except BrowserProfileError:
            continue
        if norm:
            candidates.add(norm)

    collapsed: List[str] = []
    for dom in sorted(candidates, key=lambda d: (d.count("."), len(d), d)):
        if any(dom == parent or dom.endswith("." + parent) for parent in collapsed):
            continue
        collapsed.append(dom)
    return sorted(collapsed)


def summarize_state(state_path: Path = DEFAULT_STATE_PATH) -> Dict[str, Any]:
    """Return safe metadata summary (never exposes raw cookie or LocalStorage values)."""
    path = state_path.expanduser()
    hot_path = str(hot_reload_state_path(path))
    if not path.is_file():
        return {
            "exists": False,
            "path": str(path),
            "hot_reload_path": hot_path,
            "total_cookies": 0,
            "total_origins": 0,
            "total_local_storage_keys": 0,
            "domains": {},
            "origins": {},
        }

    now = time.time()
    state = load_storage_state(path)
    domain_stats: Dict[str, Dict[str, int]] = {}

    for item in state["cookies"]:
        dom = str(item.get("domain", "")).lstrip(".") or "unknown"
        bucket = domain_stats.setdefault(dom, {"active": 0, "expired": 0, "http_only": 0})
        exp = item.get("expires", -1)
        if isinstance(exp, (int, float)) and exp != -1 and exp <= now:
            bucket["expired"] += 1
        else:
            bucket["active"] += 1
        if item.get("httpOnly"):
            bucket["http_only"] += 1

    origin_stats: Dict[str, int] = {}
    total_ls_keys = 0
    for entry in state.get("origins", []):
        if not isinstance(entry, dict):
            continue
        orig = str(entry.get("origin", "")).strip()
        items = entry.get("localStorage", [])
        if orig and isinstance(items, list):
            origin_stats[orig] = len(items)
            total_ls_keys += len(items)

    return {
        "exists": True,
        "path": str(path.resolve()),
        "hot_reload_path": hot_path,
        "total_cookies": len(state["cookies"]),
        "total_origins": len(origin_stats),
        "total_local_storage_keys": total_ls_keys,
        "domains": dict(sorted(domain_stats.items())),
        "origins": dict(sorted(origin_stats.items())),
    }
