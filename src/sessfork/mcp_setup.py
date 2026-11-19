"""Idempotent MCP server & Agent Skill installer for Antigravity, Claude Code, and Cursor."""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from sessfork.chrome import real_user_home
from sessfork.state import (
    DEFAULT_STATE_PATH,
    PLAYWRIGHT_OUTPUT_DIRNAME,
    load_storage_state,
    save_storage_state_atomic,
)

MCP_SERVER_NAME = "sessfork-browser"
DEFAULT_PLAYWRIGHT_MCP_PKG = "@playwright/mcp@0.0.85"


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


def _build_mcp_env(cmd: str) -> Dict[str, str]:
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
    if sys.platform == "darwin":
        pw_cache = real_user_home() / "Library" / "Caches" / "ms-playwright"
        if pw_cache.is_dir():
            env_map["PLAYWRIGHT_BROWSERS_PATH"] = str(pw_cache)
    return env_map


def build_playwright_mcp_entry(
    state_path: Path = DEFAULT_STATE_PATH,
    npx_cmd: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a clean stdio MCP entry (omits 'type' key for Antigravity schema compatibility).

    `--output-dir` keeps Playwright MCP snapshots/screenshots under `~/.sessfork/playwright-output`
    instead of a `.playwright-mcp/` folder in whatever project the agent happens to run in.
    """
    resolved_state = state_path.expanduser().resolve()
    output_dir = resolved_state.parent / PLAYWRIGHT_OUTPUT_DIRNAME
    cmd = npx_cmd or resolve_npx_command()
    mcp_pkg = os.environ.get("SESSFORK_PLAYWRIGHT_MCP_PKG", DEFAULT_PLAYWRIGHT_MCP_PKG).strip() or DEFAULT_PLAYWRIGHT_MCP_PKG
    return {
        "command": cmd,
        "args": [
            "-y",
            mcp_pkg,
            "--headless",
            "--isolated",
            "--caps=storage",
            f"--storage-state={resolved_state}",
            f"--output-dir={output_dir}",
        ],
        "env": _build_mcp_env(cmd),
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
) -> List[Path]:
    """Idempotently register `sessfork-browser` in each target's MCP configuration."""
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

    entry = build_playwright_mcp_entry(state_path=resolved_state, npx_cmd=npx_cmd)
    updated_files: List[Path] = []

    seen: set[Path] = set()
    for target in targets:
        if target == "none":
            continue
        for cfg_path in target_mcp_config_paths(target, home=home):
            if cfg_path in seen:
                continue
            seen.add(cfg_path)
            if _upsert_mcp_json(cfg_path, MCP_SERVER_NAME, entry):
                updated_files.append(cfg_path)

    return updated_files


def _upsert_mcp_json(cfg_path: Path, server_name: str, entry: Dict[str, Any]) -> bool:
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
3. Security rule: NEVER read, print, or echo `~/.sessfork/state.json`, `~/.sessfork/playwright-output/.sessfork-state.json`, or `~/.sessfork/keys.json` into the conversation context.
"""
