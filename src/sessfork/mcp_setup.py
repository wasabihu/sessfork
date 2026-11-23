"""Idempotent MCP server & Agent Skill installer for Antigravity, Claude Code, and Cursor."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import platform as platform_mod
import re
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple
from urllib.parse import urlparse

from sessfork.chrome import BrowserProfileError, real_user_home
from sessfork.state import (
    DEFAULT_STATE_PATH,
    PLAYWRIGHT_OUTPUT_DIRNAME,
    load_storage_state,
    save_storage_state_atomic,
)

MCP_SERVER_NAME = "sessfork-browser"
DEFAULT_PLAYWRIGHT_MCP_PKG = "@playwright/mcp@0.0.85"
BROWSER_EXECUTABLE_ENV = "SESSFORK_BROWSER_EXECUTABLE_PATH"
CDP_ENDPOINT_ENV = "SESSFORK_CDP_ENDPOINT"
_DISABLE_PINNING_VALUES = frozenset({"system", "none"})
_DISABLE_CDP_VALUES = frozenset({"none", "local", "off"})
_HEADLESS_SHELL_DIR_RE = re.compile(r"^chromium_headless_shell-(\d+)$")


def normalize_cdp_endpoint(raw: str) -> Optional[str]:
    """Validate and normalize an external CDP endpoint URL (e.g. 'http://127.0.0.1:9222' for Moli).

    Returns `None` when given 'local', 'none', 'off', or an empty string.
    """
    text = raw.strip()
    if not text or text.lower() in _DISABLE_CDP_VALUES:
        return None
    if any(ch in text for ch in " \t\r\n;|&$`'\"\\<>"):
        raise BrowserProfileError(f"Invalid characters in --cdp-endpoint URL: {raw!r}")
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https", "ws", "wss") or not parsed.hostname:
        raise BrowserProfileError(
            f"Invalid --cdp-endpoint URL {raw!r}; expected http(s):// or ws(s):// with a host "
            "(e.g. 'http://127.0.0.1:9222' for `moli serve`, or 'local' to reset)."
        )
    return text.rstrip("/")


def _headless_shell_relative_candidates(plat: str, arch: str) -> Tuple[str, ...]:
    if plat == "darwin":
        if arch in ("arm64", "aarch64"):
            return ("chrome-headless-shell-mac-arm64/chrome-headless-shell",)
        if arch in ("x86_64", "amd64", "x64"):
            return ("chrome-headless-shell-mac-x64/chrome-headless-shell",)
        return ()
    if plat.startswith("linux"):
        if arch in ("x86_64", "amd64", "x64"):
            return (
                "chrome-headless-shell-linux64/chrome-headless-shell",
                "chrome-linux/headless_shell",
            )
        if arch in ("arm64", "aarch64"):
            return (
                "chrome-headless-shell-linux-arm64/chrome-headless-shell",
                "chrome-linux/headless_shell",
            )
        return ()
    return ()


def find_playwright_headless_shell(
    cache_dir: Optional[Path] = None,
    platform: Optional[str] = None,
    machine: Optional[str] = None,
) -> Optional[Path]:
    """Locate the highest-revision Playwright chrome-headless-shell executable for the host arch.

    Respects `SESSFORK_BROWSER_EXECUTABLE_PATH` (`"system"` / `"none"` disables auto-pinning; an
    explicit executable file path overrides discovery) and `PLAYWRIGHT_BROWSERS_PATH`.
    """
    override = os.environ.get(BROWSER_EXECUTABLE_ENV, "").strip()
    if override:
        if override.lower() in _DISABLE_PINNING_VALUES:
            return None
        cand = Path(override).expanduser()
        if cand.is_file() and os.access(cand, os.X_OK):
            return cand.resolve()
        return None

    plat = (platform or sys.platform).lower()
    arch = (machine or platform_mod.machine()).lower()
    rel_candidates = _headless_shell_relative_candidates(plat, arch)
    if not rel_candidates:
        return None

    rel_cache = (
        Path("Library") / "Caches" / "ms-playwright"
        if plat == "darwin"
        else Path(".cache") / "ms-playwright"
    )
    if cache_dir is not None:
        root_dirs = [Path(cache_dir).expanduser()]
    else:
        env_browsers = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
        if env_browsers and env_browsers != "0":
            root_dirs = [Path(env_browsers).expanduser()]
        else:
            real_home = real_user_home()
            root_dirs = [real_home / rel_cache]
            if not os.environ.get("SESSFORK_HOME"):
                env_home = Path.home().expanduser()
                if env_home.resolve() != real_home.resolve():
                    root_dirs.append(env_home / rel_cache)

    for root_dir in root_dirs:
        if not root_dir.is_dir():
            continue
        revisions: List[Tuple[int, Path]] = []
        try:
            entries = list(root_dir.iterdir())
        except OSError:
            continue

        for entry in entries:
            match = _HEADLESS_SHELL_DIR_RE.match(entry.name)
            if not match or entry.is_symlink() or not entry.is_dir():
                continue
            revisions.append((int(match.group(1)), entry))

        revisions.sort(key=lambda item: item[0], reverse=True)
        for _, rev_dir in revisions:
            for rel in rel_candidates:
                rel_path = Path(rel)
                sub_dir = rev_dir / rel_path.parent
                cand = rev_dir / rel_path
                if sub_dir.is_symlink() or cand.is_symlink():
                    continue
                if cand.is_file() and os.access(cand, os.X_OK):
                    return cand.resolve()
    return None


def resolve_npx_command() -> str:
    """Resolve an absolute npx path so macOS GUI-launched agents never hit minimal $PATH bugs."""
    found = shutil.which("npx")
    if found:
        return found
    for candidate in (
        "/opt/homebrew/bin/npx",
        "/usr/local/bin/npx",
        str(real_user_home() / ".volta" / "bin" / "npx"),
    ):
        if Path(candidate).is_file():
            return candidate
    return "npx"


def _build_mcp_env(cmd: str, platform: Optional[str] = None) -> Dict[str, str]:
    """Build a deterministic PATH (and macOS Playwright browser cache) for GUI-launched MCP servers."""
    candidates: List[str] = []
    cmd_path = Path(cmd)
    if cmd_path.is_absolute():
        candidates.append(str(cmd_path.parent))
        if cmd_path.exists():
            candidates.append(str(cmd_path.resolve().parent))
    candidates.extend(
        (
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin",
        )
    )
    path_entries: List[str] = []
    for entry in candidates:
        if entry and entry not in path_entries:
            path_entries.append(entry)

    env_map: Dict[str, str] = {"PATH": ":".join(path_entries)}
    plat = (platform or sys.platform).lower()
    if plat == "darwin":
        pw_env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
        pw_cache = (
            Path(pw_env).expanduser()
            if (pw_env and pw_env != "0")
            else (real_user_home() / "Library" / "Caches" / "ms-playwright")
        )
        if pw_cache.is_dir():
            env_map["PLAYWRIGHT_BROWSERS_PATH"] = str(pw_cache)
    return env_map


def build_playwright_mcp_entry(
    state_path: Path = DEFAULT_STATE_PATH,
    npx_cmd: Optional[str] = None,
    platform: Optional[str] = None,
    cache_dir: Optional[Path] = None,
    machine: Optional[str] = None,
    cdp_endpoint: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a clean stdio MCP entry (omits 'type' key for Antigravity schema compatibility).

    `--output-dir` keeps Playwright MCP snapshots/screenshots under `~/.sessfork/playwright-output`
    instead of a `.playwright-mcp/` folder in whatever project the agent happens to run in.
    When `cdp_endpoint` (or `SESSFORK_CDP_ENDPOINT`) is provided, attaches to an external CDP
    server such as `moli serve` (`http://127.0.0.1:9222`) instead of launching local Chromium.
    Otherwise on macOS (or when `SESSFORK_BROWSER_EXECUTABLE_PATH` is explicitly set), pins
    Playwright's `chrome-headless-shell` via `--executable-path` to avoid system Chrome headless
    multi-origin `storageState` hydration crashes (`Mach rendezvous failed`).
    """
    resolved_state = state_path.expanduser().resolve()
    output_dir = resolved_state.parent / PLAYWRIGHT_OUTPUT_DIRNAME
    cmd = npx_cmd or resolve_npx_command()
    mcp_pkg = os.environ.get("SESSFORK_PLAYWRIGHT_MCP_PKG", DEFAULT_PLAYWRIGHT_MCP_PKG).strip() or DEFAULT_PLAYWRIGHT_MCP_PKG
    plat = (platform or sys.platform).lower()

    if cdp_endpoint is not None:
        normalized_cdp = normalize_cdp_endpoint(cdp_endpoint)
    else:
        env_cdp = os.environ.get(CDP_ENDPOINT_ENV, "").strip()
        normalized_cdp = normalize_cdp_endpoint(env_cdp) if env_cdp else None

    if normalized_cdp is not None:
        args = [
            "-y",
            mcp_pkg,
            f"--cdp-endpoint={normalized_cdp}",
            "--caps=storage",
            f"--output-dir={output_dir}",
        ]
        return {
            "command": cmd,
            "args": args,
            "env": _build_mcp_env(cmd, platform=plat),
        }

    args = [
        "-y",
        mcp_pkg,
        "--headless",
        "--isolated",
        "--caps=storage",
        f"--storage-state={resolved_state}",
        f"--output-dir={output_dir}",
    ]
    override = os.environ.get(BROWSER_EXECUTABLE_ENV, "").strip()
    has_explicit_override = bool(override and override.lower() not in _DISABLE_PINNING_VALUES)
    if plat == "darwin" or has_explicit_override:
        exec_path = find_playwright_headless_shell(
            cache_dir=cache_dir,
            platform=plat,
            machine=machine,
        )
        if exec_path is not None:
            args.append(f"--executable-path={exec_path}")
    return {
        "command": cmd,
        "args": args,
        "env": _build_mcp_env(cmd, platform=plat),
    }


def _extract_cli_arg(args: Any, flag: str) -> Optional[str]:
    if not isinstance(args, list):
        return None
    prefix = f"{flag}="
    for idx, item in enumerate(args):
        if not isinstance(item, str):
            continue
        if item.startswith(prefix):
            val = item.split("=", 1)[1].strip()
            return val or None
        if item == flag and idx + 1 < len(args) and isinstance(args[idx + 1], str):
            val = args[idx + 1].strip()
            return val or None
    return None


def _extract_executable_path_arg(args: Any) -> Optional[str]:
    return _extract_cli_arg(args, "--executable-path")


def _extract_cdp_endpoint_arg(args: Any) -> Optional[str]:
    return _extract_cli_arg(args, "--cdp-endpoint")


def _read_existing_cdp_endpoint(cfg_path: Path) -> Optional[str]:
    if not cfg_path.is_file():
        return None
    try:
        raw = cfg_path.read_text(encoding="utf-8").strip()
        doc = json.loads(raw) if raw else {}
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(doc, dict):
        return None
    servers = doc.get("mcpServers")
    if not isinstance(servers, dict):
        return None
    entry = servers.get(MCP_SERVER_NAME)
    if not isinstance(entry, dict):
        return None
    return _extract_cdp_endpoint_arg(entry.get("args"))


def inspect_mcp_headless_shell(
    config_paths: Optional[Iterable[Path]] = None,
    target: str = "auto",
    home: Optional[Path] = None,
    cache_dir: Optional[Path] = None,
    platform: Optional[str] = None,
    machine: Optional[str] = None,
) -> Dict[str, Any]:
    """Inspect chrome-headless-shell detection, pinned `--executable-path`, and `--cdp-endpoint` configs."""
    plat = (platform or sys.platform).lower()
    override = os.environ.get(BROWSER_EXECUTABLE_ENV, "").strip()
    auto_pin_disabled = override.lower() in _DISABLE_PINNING_VALUES
    has_explicit_override = bool(override and not auto_pin_disabled)
    detected = find_playwright_headless_shell(
        cache_dir=cache_dir,
        platform=plat,
        machine=machine,
    )
    paths = list(config_paths) if config_paths is not None else target_mcp_config_paths(target, home=home)

    pinned_configs: List[str] = []
    unpinned_configs: List[str] = []
    stale_configs: List[str] = []
    cdp_configs: Dict[str, str] = {}
    seen: set[Path] = set()

    for cfg_path in paths:
        if cfg_path in seen or not cfg_path.is_file():
            continue
        seen.add(cfg_path)
        try:
            raw = cfg_path.read_text(encoding="utf-8").strip()
            doc = json.loads(raw) if raw else {}
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(doc, dict):
            continue
        servers = doc.get("mcpServers")
        if not isinstance(servers, dict):
            continue
        entry = servers.get(MCP_SERVER_NAME)
        if not isinstance(entry, dict):
            continue
        cdp_ep = _extract_cdp_endpoint_arg(entry.get("args"))
        if cdp_ep:
            cdp_configs[str(cfg_path)] = cdp_ep
            continue
        pinned_exec = _extract_executable_path_arg(entry.get("args"))
        if not pinned_exec:
            unpinned_configs.append(str(cfg_path))
        else:
            exec_file = Path(pinned_exec).expanduser()
            if exec_file.is_file() and os.access(exec_file, os.X_OK):
                pinned_configs.append(str(cfg_path))
            else:
                stale_configs.append(str(cfg_path))

    all_wired_use_cdp = bool(cdp_configs) and not (pinned_configs or unpinned_configs or stale_configs)
    warning: Optional[str] = None
    if stale_configs:
        if auto_pin_disabled:
            warning = (
                f"Wired MCP config(s) reference a missing --executable-path ({', '.join(stale_configs)}). "
                "Run `sessfork --setup` to remove the stale flag, then reload MCP."
            )
        elif has_explicit_override and detected is None:
            warning = (
                f"Wired MCP config(s) reference a missing --executable-path ({', '.join(stale_configs)}), "
                f"and {BROWSER_EXECUTABLE_ENV}={override!r} does not point to an executable file. "
                f"Fix or unset {BROWSER_EXECUTABLE_ENV}, then run `sessfork --setup` and reload MCP."
            )
        elif detected is not None:
            warning = (
                f"Wired MCP config(s) reference a missing --executable-path ({', '.join(stale_configs)}). "
                f"Run `sessfork --setup` to update to {detected}, then reload MCP."
            )
        else:
            warning = (
                f"Wired MCP config(s) reference a missing --executable-path ({', '.join(stale_configs)}). "
                "Run `npx -y playwright-core install chromium-headless-shell` followed by `sessfork --setup`, then reload MCP."
            )
    elif has_explicit_override and detected is None and not all_wired_use_cdp:
        warning = (
            f"{BROWSER_EXECUTABLE_ENV}={override!r} does not point to an executable file; "
            "--executable-path will be omitted. Fix or unset "
            f"{BROWSER_EXECUTABLE_ENV} (or set it to 'system'/'none' to disable auto-pinning)."
        )
    elif plat == "darwin" and not auto_pin_disabled and not all_wired_use_cdp:
        if detected is None:
            warning = (
                "Playwright chrome-headless-shell not found. On macOS, system Chrome in headless mode "
                "can crash when loading multi-origin storageState ('Mach rendezvous failed'). "
                "Install it with `npx -y playwright-core install chromium-headless-shell` followed by `sessfork --setup`."
            )
        elif unpinned_configs:
            warning = (
                f"Wired MCP config(s) do not pin --executable-path ({', '.join(unpinned_configs)}). "
                "On macOS, run `sessfork --setup` to pin chrome-headless-shell and avoid headless Chrome "
                "multi-origin crashes, then reload MCP."
            )

    return {
        "playwright_headless_shell": str(detected) if detected is not None else None,
        "auto_pin_disabled": auto_pin_disabled,
        "pinned_mcp_configs": pinned_configs,
        "unpinned_mcp_configs": unpinned_configs,
        "stale_mcp_configs": stale_configs,
        "cdp_mcp_configs": cdp_configs,
        "warning": warning,
    }


def detect_installed_targets(home: Optional[Path] = None) -> List[str]:
    """Auto-detect installed AI coding agents on the user's machine."""
    root = (home or real_user_home()).expanduser()
    targets: List[str] = ["antigravity"]
    if (
        (root / ".claude.json").is_file()
        or (root / ".claude").is_dir()
        or (home is None and shutil.which("claude") is not None)
    ):
        targets.append("claude")
    if (root / ".cursor").is_dir() or (home is None and Path("/Applications/Cursor.app").exists()):
        targets.append("cursor")
    return targets


def target_mcp_config_paths(target: str, home: Optional[Path] = None) -> List[Path]:
    """Return MCP config JSON file paths for the requested agent target."""
    root = (home or real_user_home()).expanduser()
    if target == "auto":
        auto_paths: List[Path] = []
        for sub_target in detect_installed_targets(home=home):
            for p in target_mcp_config_paths(sub_target, home=home):
                if p not in auto_paths:
                    auto_paths.append(p)
        return auto_paths
    if target == "antigravity":
        paths = [root / ".gemini" / "config" / "mcp_config.json"]
        legacy_cli = root / ".gemini" / "antigravity-cli" / "mcp_config.json"
        if legacy_cli.is_file():
            paths.append(legacy_cli)
        if home is None:
            env_home = Path.home().expanduser()
            if env_home.resolve() != root.resolve() and (env_home / ".gemini").is_dir():
                env_cfg = env_home / ".gemini" / "config" / "mcp_config.json"
                if env_cfg not in paths:
                    paths.append(env_cfg)
        return paths
    if target == "claude":
        return [root / ".claude.json"]
    if target == "cursor":
        return [root / ".cursor" / "mcp.json"]
    if target == "all":
        all_paths: List[Path] = []
        for sub_target in ("antigravity", "claude", "cursor"):
            for p in target_mcp_config_paths(sub_target, home=home):
                if p not in all_paths:
                    all_paths.append(p)
        return all_paths
    return []


def ensure_mcp_configured(
    targets: Iterable[str],
    state_path: Path = DEFAULT_STATE_PATH,
    home: Optional[Path] = None,
    npx_cmd: Optional[str] = None,
    cdp_endpoint: Optional[str] = None,
    preserve_existing_cdp: bool = True,
) -> List[Path]:
    """Idempotently register `sessfork-browser` in each target's MCP configuration."""
    # Validate explicit or env CDP endpoint before touching any files
    if cdp_endpoint is not None:
        normalize_cdp_endpoint(cdp_endpoint)
    else:
        env_cdp = os.environ.get(CDP_ENDPOINT_ENV, "").strip()
        if env_cdp:
            normalize_cdp_endpoint(env_cdp)

    # Ensure the state file exists on disk so Playwright MCP never errors on startup
    resolved_state = state_path.expanduser().resolve()
    if not resolved_state.is_file():
        save_storage_state_atomic(load_storage_state(resolved_state), resolved_state)
    output_dir = resolved_state.parent / PLAYWRIGHT_OUTPUT_DIRNAME
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    for secure_dir in (resolved_state.parent, output_dir):
        try:
            os.chmod(secure_dir, 0o700)
        except OSError:
            pass

    default_entry = build_playwright_mcp_entry(
        state_path=resolved_state,
        npx_cmd=npx_cmd,
        cdp_endpoint=cdp_endpoint,
    )
    has_env_cdp = bool(os.environ.get(CDP_ENDPOINT_ENV, "").strip())
    updated_files: List[Path] = []

    seen: set[Path] = set()
    for target in targets:
        if target == "none":
            continue
        for cfg_path in target_mcp_config_paths(target, home=home):
            if cfg_path in seen:
                continue
            seen.add(cfg_path)
            entry = default_entry
            if preserve_existing_cdp and cdp_endpoint is None and not has_env_cdp:
                existing_cdp = _read_existing_cdp_endpoint(cfg_path)
                if existing_cdp:
                    entry = build_playwright_mcp_entry(
                        state_path=resolved_state,
                        npx_cmd=npx_cmd,
                        cdp_endpoint=existing_cdp,
                    )
            if _upsert_mcp_json(cfg_path, MCP_SERVER_NAME, entry):
                updated_files.append(cfg_path)

    return updated_files


@contextlib.contextmanager
def _exclusive_mcp_cfg_lock(cfg_path: Path) -> Iterator[None]:
    """Serialize concurrent read-modify-write updates to an agent MCP config file."""
    resolved = cfg_path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    lock_path = resolved.parent / f".{resolved.name}.sessfork.lock"
    if lock_path.is_symlink():
        raise OSError(f"Refusing to use symlinked MCP config lock file: {lock_path}")
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


def _upsert_mcp_json(cfg_path: Path, server_name: str, entry: Dict[str, Any]) -> bool:
    with _exclusive_mcp_cfg_lock(cfg_path):
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        doc: Dict[str, Any] = {}
        file_mode = 0o600
        if cfg_path.is_file():
            file_mode = stat.S_IMODE(cfg_path.stat().st_mode)
            raw = cfg_path.read_text(encoding="utf-8").strip()
            if raw:
                doc = json.loads(raw)

        servers = doc.get("mcpServers")
        if not isinstance(servers, dict):
            servers = {}
            doc["mcpServers"] = servers

        if servers.get(server_name) == entry:
            return False

        servers[server_name] = entry
        payload = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
        fd, tmp_name = tempfile.mkstemp(prefix=".mcp_", suffix=".tmp", dir=str(cfg_path.parent))
        try:
            os.fchmod(fd, file_mode)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp_name, cfg_path)
        except BaseException:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise
        return True


def bundled_skill_markdown() -> str:
    """Return the canonical SKILL.md content for sessfork."""
    repo_skill = Path(__file__).resolve().parents[2] / "skills" / "sessfork" / "SKILL.md"
    if repo_skill.is_file():
        return repo_skill.read_text(encoding="utf-8")
    return _FALLBACK_SKILL_MD


def install_agent_skills(home: Optional[Path] = None) -> List[Path]:
    """Install SKILL.md into Antigravity (~/.gemini/skills), Claude Code, and universal (~/.agents/skills)."""
    root = (home or real_user_home()).expanduser()
    content = bundled_skill_markdown()
    destinations = [
        root / ".gemini" / "skills" / "sessfork" / "SKILL.md",
        root / ".agents" / "skills" / "sessfork" / "SKILL.md",
    ]
    if (root / ".claude").is_dir():
        destinations.append(root / ".claude" / "skills" / "sessfork" / "SKILL.md")
    if home is None:
        env_home = Path.home().expanduser()
        if env_home.resolve() != root.resolve() and (env_home / ".gemini" / "config" / "skills").is_dir():
            destinations.append(env_home / ".gemini" / "config" / "skills" / "sessfork" / "SKILL.md")
    written: List[Path] = []
    seen: set[Path] = set()
    for dest in destinations:
        if dest in seen:
            continue
        seen.add(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.is_file() or dest.read_text(encoding="utf-8") != content:
            dest.write_text(content, encoding="utf-8")
            written.append(dest)
    return written


_FALLBACK_SKILL_MD = """---
name: sessfork
description: Fork live Chrome login sessions (Cookies + LocalStorage) into a background headless Playwright MCP browser (`sessfork-browser`) so you can browse authenticated sites without stealing or interrupting the user's Chrome window focus.
---
# sessfork — Zero-Focus-Theft Authenticated Browser Skill

Use `sessfork` whenever a task requires reading or interacting with a website where the user is already signed in on their local Chrome, WITHOUT hijacking their active Chrome window.

## Workflow
1. Fork or refresh the needed domain(s) from live Chrome into `~/.sessfork/state.json`:
   ```bash
   sessfork <domain1> [domain2 ...]
   sessfork --refresh   # Re-fork all currently saved domains
   ```
2. Hot-reload into a running `sessfork-browser` without restarting MCP by calling `browser_set_storage_state` with `filename` set to `~/.sessfork/playwright-output/.sessfork-state.json` (the exact `Hot-reload path` printed by `sessfork --status`), then run `browser_navigate`, `browser_snapshot`, etc.
3. On macOS, `sessfork --setup` automatically pins Playwright's `chrome-headless-shell` via `--executable-path` to avoid system Chrome headless multi-origin crashes (`Target page, context or browser has been closed` / `Mach rendezvous failed`). If that error occurs, run `npx -y playwright-core install chromium-headless-shell && sessfork --setup` and reload MCP once.
4. Security rule: NEVER read, print, or echo `~/.sessfork/state.json`, `~/.sessfork/playwright-output/.sessfork-state.json`, or `~/.sessfork/keys.json` into the conversation context.
"""
