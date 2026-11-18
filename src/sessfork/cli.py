"""One-command CLI entry point for sessfork (Cookies + LevelDB LocalStorage)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from sessfork import __version__
from sessfork.chrome import BrowserProfileError, extract_browser_session
from sessfork.crypto import (
    BROWSER_KEYRING_MAP,
    CryptoError,
    clear_cached_aes_keys,
    list_cached_key_browsers,
)
from sessfork.localstorage import LevelDBFormatError
from sessfork.mcp_setup import (
    MCP_SERVER_NAME,
    ensure_mcp_configured,
    install_agent_skills,
    target_mcp_config_paths,
)
from sessfork.state import (
    DEFAULT_STATE_PATH,
    clear_state,
    summarize_state,
    upsert_session_state,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sessfork",
        description=(
            "Fork live Chrome auth state (SQLite Cookies + LevelDB LocalStorage) into a "
            "background headless MCP browser (Antigravity, Claude Code, Cursor) with zero window focus theft."
        ),
        epilog=(
            "Examples:\n"
            "  sessfork github.com linear.app  # Fork Cookies + LocalStorage & auto-wire MCP\n"
            "  sessfork app.spa.io --no-cookies # Fork pure JWT/SPA LocalStorage without Keychain prompt\n"
            "  sessfork --setup                # One-click wire MCP config + install Agent SKILL.md\n"
            "  sessfork --status               # Show forked Cookie domains & LocalStorage origins\n"
            "  sessfork --clear linear.app     # Remove forked Cookies & LocalStorage for linear.app"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "domains",
        nargs="*",
        help="Domain(s) or URL(s) to fork from your live browser (e.g. github.com linear.app)",
    )
    parser.add_argument(
        "-b",
        "--browser",
        choices=sorted(BROWSER_KEYRING_MAP.keys()),
        default="chrome",
        help="Source Chromium browser (default: chrome)",
    )
    parser.add_argument(
        "-p",
        "--profile",
        default=None,
        help="Browser profile folder name (e.g. 'Default', 'Profile 1'; auto-detected if omitted)",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="Explicit path to a Chromium Cookies SQLite file",
    )
    parser.add_argument(
        "--leveldb-path",
        type=Path,
        default=None,
        help="Explicit path to a Chromium Local Storage/leveldb directory",
    )
    parser.add_argument(
        "--no-cookies",
        action="store_true",
        help="Skip SQLite Cookies extraction (extract LevelDB LocalStorage only, zero Keychain prompt)",
    )
    parser.add_argument(
        "--no-local-storage",
        action="store_true",
        help="Skip LevelDB LocalStorage extraction (extract SQLite Cookies only)",
    )
    parser.add_argument(
        "-s",
        "--state-file",
        type=Path,
        default=DEFAULT_STATE_PATH,
        help=f"Playwright storageState output path (default: {DEFAULT_STATE_PATH})",
    )
    parser.add_argument(
        "-t",
        "--target",
        choices=("auto", "antigravity", "claude", "cursor", "all", "none"),
        default="auto",
        help="Agent MCP config to auto-wire (default: auto-detect Antigravity/Claude/Cursor; use 'none' to skip)",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Replace existing state.json instead of incrementally merging domains",
    )
    parser.add_argument(
        "--all-domains",
        action="store_true",
        help="Fork all active cookies and LocalStorage origins from the browser profile (use with caution)",
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Install headless MCP config and global SKILL.md for one-sentence agent usage",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Inspect currently forked domains, LocalStorage origins, and MCP status",
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Clear saved cookies and LocalStorage origins (for specified domains, or all if omitted)",
    )
    parser.add_argument(
        "--cache-key",
        action="store_true",
        help=(
            "Opt-in: cache the derived Safe Storage key in ~/.sessfork/keys.json (0600) to skip "
            "future Keychain prompts. Trade-off: any process running as your user could read it."
        ),
    )
    parser.add_argument(
        "--refresh-key",
        action="store_true",
        help="Force re-reading Safe Storage key from OS Keychain (updates the cache if --cache-key is set)",
    )
    parser.add_argument(
        "--purge-keys",
        action="store_true",
        help="Remove cached derived Safe Storage keys (~/.sessfork/keys.json)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit machine-readable JSON summary (never includes secret cookie/token values)",
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"sessfork {__version__}",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    state_path: Path = args.state_file.expanduser()

    if args.clear:
        rem_c, left_c, rem_o, left_o = clear_state(args.domains, state_path=state_path)
        payload = {
            "action": "clear",
            "removed_cookies": rem_c,
            "remaining_cookies": left_c,
            "removed_origins": rem_o,
            "remaining_origins": left_o,
            "state_file": str(state_path),
        }
        msg = (
            f"Cleared {rem_c} cookie(s) and {rem_o} LocalStorage origin(s); "
            f"{left_c} cookie(s) and {left_o} origin(s) remaining in {state_path}"
        )
        if args.purge_keys:
            purged = clear_cached_aes_keys()
            payload["purged_keys"] = purged
            msg += f"; purged {purged} cached Safe Storage key(s)"
        _emit(payload, args.as_json, msg)
        return 0

    if args.purge_keys:
        purged = clear_cached_aes_keys()
        payload = {"action": "purge_keys", "purged_keys": purged}
        _emit(payload, args.as_json, f"Purged {purged} cached Safe Storage key(s).")
        return 0

    if args.status:
        info = summarize_state(state_path)
        wired = [str(p) for p in target_mcp_config_paths(args.target) if p.is_file()]
        info["wired_mcp_configs"] = wired
        info["cached_key_browsers"] = list_cached_key_browsers()
        _emit_status(info, args.as_json)
        return 0

    if args.no_cookies and args.no_local_storage:
        print("sessfork error: cannot pass both --no-cookies and --no-local-storage", file=sys.stderr)
        return 1

    did_work = False
    updated_mcp: List[Path] = []
    installed_skills: List[Path] = []

    try:
        if args.setup:
            updated_mcp = ensure_mcp_configured([args.target], state_path=state_path)
            installed_skills = install_agent_skills()
            did_work = True

        if args.domains or args.all_domains:
            domains = [] if args.all_domains else args.domains
            cookies, origins, profile_label = extract_browser_session(
                domains=domains,
                browser=args.browser,
                profile=args.profile,
                db_path=args.db_path,
                leveldb_path=args.leveldb_path,
                include_cookies=not args.no_cookies,
                include_local_storage=not args.no_local_storage,
                refresh_key=args.refresh_key,
                cache_key=args.cache_key,
            )
            saved_path, up_cookies, tot_cookies, up_ls_keys, tot_origins = upsert_session_state(
                new_cookies=cookies,
                new_origins=None if args.no_local_storage else origins,
                refreshed_domains=domains,
                state_path=state_path,
                replace_all=args.replace,
            )
            if args.target != "none":
                more_mcp = ensure_mcp_configured([args.target], state_path=saved_path)
                for p in more_mcp:
                    if p not in updated_mcp:
                        updated_mcp.append(p)
                more_skills = install_agent_skills()
                for sk in more_skills:
                    if sk not in installed_skills:
                        installed_skills.append(sk)
        else:
            saved_path = state_path
            up_cookies = tot_cookies = up_ls_keys = tot_origins = 0
            profile_label = ""
    except (CryptoError, BrowserProfileError, LevelDBFormatError, OSError) as exc:
        if args.as_json:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"sessfork error: {exc}", file=sys.stderr)
        return 1

    if args.domains or args.all_domains:

        result = {
            "ok": True,
            "browser": args.browser,
            "source_profile": profile_label,
            "forked_cookies": up_cookies,
            "total_cached_cookies": tot_cookies,
            "forked_local_storage_keys": up_ls_keys,
            "total_cached_origins": tot_origins,
            "state_file": str(saved_path),
            "mcp_server": MCP_SERVER_NAME,
            "updated_mcp_configs": [str(p) for p in updated_mcp],
            "installed_skills": [str(p) for p in installed_skills],
        }
        if args.as_json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(
                f"Forked {up_cookies} cookie(s) + {up_ls_keys} LocalStorage key(s) "
                f"from {args.browser} ({profile_label}) -> {saved_path} "
                f"({tot_cookies} cookies, {tot_origins} origins cached)."
            )
            if up_cookies == 0 and up_ls_keys == 0:
                print(
                    "Note: 0 matching active cookies or LocalStorage keys found. "
                    f"Check that you are signed in to {', '.join(args.domains)} in {args.browser}.",
                    file=sys.stderr,
                )
            for cfg in updated_mcp:
                print(f"Wired headless MCP '{MCP_SERVER_NAME}' in {cfg}")
            for sk in installed_skills:
                print(f"Installed Agent Skill in {sk}")
        return 0

    if did_work:
        payload = {
            "ok": True,
            "action": "setup",
            "state_file": str(state_path.resolve()),
            "mcp_server": MCP_SERVER_NAME,
            "updated_mcp_configs": [str(p) for p in updated_mcp],
            "installed_skills": [str(p) for p in installed_skills],
        }
        if args.as_json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print(f"sessfork setup complete (state path: {state_path.resolve()}).")
            for cfg in updated_mcp:
                print(f"  + Updated MCP config: {cfg}")
            for sk in installed_skills:
                print(f"  + Installed SKILL.md: {sk}")
            if not updated_mcp and not installed_skills:
                print("  = MCP config and SKILL.md were already up to date.")
        return 0

    print(
        "Quick Start:\n"
        "  sessfork github.com              # Fork Chrome login & auto-wire MCP + Skill\n"
        "  sessfork app.spa.io --no-cookies # Fork LocalStorage JWT only (no Keychain prompt)\n"
        "  sessfork --setup                 # One-click wire MCP config & Agent SKILL.md\n"
        "  sessfork --status                # Inspect cached domains & origins\n"
    )
    parser.print_help()
    return 0


def _emit(payload: dict, as_json: bool, text: str) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(text)


def _emit_status(info: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(info, indent=2, ensure_ascii=False))
        return
    if not info["exists"]:
        print(f"No state file yet at {info['path']} (run `sessfork <domain>` to create one).")
        if info.get("cached_key_browsers"):
            print(
                "Cached Safe Storage key(s): "
                f"{', '.join(info['cached_key_browsers'])} (used only with --cache-key; "
                "remove with `sessfork --purge-keys`)"
            )
        return
    print(
        f"State file: {info['path']} "
        f"({info['total_cookies']} cookies, {info['total_local_storage_keys']} LocalStorage keys "
        f"across {info['total_origins']} origin(s))"
    )
    if info["domains"]:
        print("Cookies:")
        for dom, counts in info["domains"].items():
            print(
                f"  - {dom}: {counts['active']} active, "
                f"{counts['http_only']} HttpOnly, {counts['expired']} expired"
            )
    if info.get("origins"):
        print("LocalStorage origins:")
        for orig, count in info["origins"].items():
            print(f"  - {orig}: {count} key(s)")
    if info.get("cached_key_browsers"):
        print(
            "Cached Safe Storage key(s): "
            f"{', '.join(info['cached_key_browsers'])} (used only with --cache-key; "
            "remove with `sessfork --purge-keys`)"
        )
    if info.get("wired_mcp_configs"):
        print(f"Wired MCP config(s): {', '.join(info['wired_mcp_configs'])}")


if __name__ == "__main__":
    sys.exit(main())
