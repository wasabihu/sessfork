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
    VALID_ENGINES,
    ensure_mcp_configured,
    inspect_mcp_headless_shell,
    install_agent_skills,
    target_mcp_config_paths,
)
from sessfork.moli_bridge import run_managed_moli_mcp
from sessfork.state import (
    DEFAULT_STATE_PATH,
    clear_state,
    hot_reload_state_path,
    list_state_domains,
    summarize_state,
    upsert_session_state,
)
from sessfork.ui_server import serve_ui


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sessfork",
        description=(
            "Fork live Chrome auth state (SQLite Cookies + LevelDB LocalStorage) into a "
            "background headless MCP browser (Antigravity, Claude Code, Cursor, OpenCode) with zero window focus theft."
        ),
        epilog=(
            "Examples:\n"
            "  sessfork github.com linear.app  # Fork Cookies + LocalStorage & auto-wire MCP\n"
            "  sessfork app.spa.io --no-cookies # Fork pure JWT/SPA LocalStorage without Keychain prompt\n"
            "  sessfork --refresh              # Re-fork all domains already saved in state.json\n"
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
        choices=("auto", "antigravity", "claude", "cursor", "opencode", "all", "none"),
        default="auto",
        help="Agent MCP config to auto-wire (default: auto-detect Antigravity/Claude/Cursor/OpenCode; use 'none' to skip)",
    )
    parser.add_argument(
        "--engine",
        choices=VALID_ENGINES,
        default=None,
        help=(
            "Browser engine mode for MCP wiring: 'chrome' (default chrome-headless-shell), "
            "'moli' (auto-managed Rust Moli, zero manual `moli serve`), or "
            "'dual' (fast `sessfork-moli` + lazy 0 MB `sessfork-browser` Chrome fallback)"
        ),
    )
    parser.add_argument(
        "--with-moli",
        action="store_true",
        help="Shorthand for `--engine dual` (wires both managed `sessfork-moli` and lazy Chrome fallback)",
    )
    parser.add_argument(
        "--cdp-endpoint",
        default=None,
        help=(
            "Attach Playwright MCP to an external CDP server such as `moli serve` "
            "(e.g. 'http://127.0.0.1:9222'); pass 'local' or 'none' to switch back to local headless Chromium"
        ),
    )
    parser.add_argument(
        "--ui",
        action="store_true",
        help="Launch the local 127.0.0.1 web control panel to switch engines and manage forked domains",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="Optional loopback port for `--ui` (default: random free port)",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Do not automatically open the system browser when running `--ui`",
    )
    parser.add_argument(
        "--mcp-bridge",
        choices=("moli",),
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Replace existing state.json instead of incrementally merging domains",
    )
    parser.add_argument(
        "-r",
        "--refresh",
        action="store_true",
        help="Re-fork all domains currently saved in state.json (can be combined with new domains)",
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

    if args.mcp_bridge == "moli":
        try:
            return run_managed_moli_mcp(state_path=state_path)
        except (BrowserProfileError, OSError) as exc:
            print(f"sessfork error: {exc}", file=sys.stderr)
            return 1

    if args.ui:
        return serve_ui(
            state_path=state_path,
            target=args.target,
            port=args.port,
            open_browser=not args.no_open,
        )

    effective_engine = args.engine
    if args.with_moli:
        if effective_engine is not None and effective_engine != "dual":
            print("sessfork error: cannot combine --with-moli with a different --engine", file=sys.stderr)
            return 1
        effective_engine = "dual"

    if args.clear:
        try:
            rem_c, left_c, rem_o, left_o = clear_state(
                args.domains if args.domains else None,
                state_path=state_path,
            )
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
        except (CryptoError, BrowserProfileError, LevelDBFormatError, OSError) as exc:
            if args.as_json:
                print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            else:
                print(f"sessfork error: {exc}", file=sys.stderr)
            return 1

    if args.purge_keys:
        try:
            purged = clear_cached_aes_keys()
            payload = {"action": "purge_keys", "purged_keys": purged}
            _emit(payload, args.as_json, f"Purged {purged} cached Safe Storage key(s).")
            return 0
        except (CryptoError, BrowserProfileError, OSError) as exc:
            if args.as_json:
                print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            else:
                print(f"sessfork error: {exc}", file=sys.stderr)
            return 1

    if args.status:
        try:
            info = summarize_state(state_path)
            config_paths = target_mcp_config_paths(args.target)
            wired = [str(p) for p in config_paths if p.is_file()]
            info["wired_mcp_configs"] = wired
            info["cached_key_browsers"] = list_cached_key_browsers()
            shell_info = inspect_mcp_headless_shell(config_paths=config_paths)
            info["playwright_headless_shell"] = shell_info["playwright_headless_shell"]
            info["auto_pin_disabled"] = shell_info["auto_pin_disabled"]
            info["pinned_mcp_configs"] = shell_info["pinned_mcp_configs"]
            info["unpinned_mcp_configs"] = shell_info["unpinned_mcp_configs"]
            info["stale_mcp_configs"] = shell_info["stale_mcp_configs"]
            info["cdp_mcp_configs"] = shell_info["cdp_mcp_configs"]
            info["engine_modes"] = shell_info["engine_modes"]
            info["moli_binary"] = shell_info["moli_binary"]
            info["moli_version"] = shell_info["moli_version"]
            info["rust_mcp_binary"] = shell_info["rust_mcp_binary"]
            info["rust_mcp_version"] = shell_info["rust_mcp_version"]
            info["warning"] = shell_info["warning"]
            _emit_status(info, args.as_json)
            return 0
        except (CryptoError, BrowserProfileError, OSError) as exc:
            if args.as_json:
                print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            else:
                print(f"sessfork error: {exc}", file=sys.stderr)
            return 1

    if args.no_cookies and args.no_local_storage:
        print("sessfork error: cannot pass both --no-cookies and --no-local-storage", file=sys.stderr)
        return 1

    has_fork_request = False
    domains: List[str] = []
    if args.all_domains:
        has_fork_request = True
        domains = []
    elif args.refresh:
        try:
            saved_domains = list_state_domains(state_path)
        except (BrowserProfileError, OSError) as exc:
            if args.as_json:
                print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            else:
                print(f"sessfork error: {exc}", file=sys.stderr)
            return 1
        combined: List[str] = []
        for dom in saved_domains + list(args.domains):
            if dom not in combined:
                combined.append(dom)
        if not combined:
            msg = f"No saved domains found in {state_path} to refresh (run 'sessfork <domain>' first)."
            if args.as_json:
                print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False))
            else:
                print(f"sessfork error: {msg}", file=sys.stderr)
            return 1
        has_fork_request = True
        domains = combined
    elif args.domains:
        has_fork_request = True
        domains = list(args.domains)

    did_work = False
    updated_mcp: List[Path] = []
    installed_skills: List[Path] = []

    try:
        if args.setup or ((args.cdp_endpoint is not None or effective_engine is not None) and not has_fork_request):
            updated_mcp = ensure_mcp_configured(
                [args.target],
                state_path=state_path,
                cdp_endpoint=args.cdp_endpoint,
                engine=effective_engine,
            )
            installed_skills = install_agent_skills()
            did_work = True

        if has_fork_request:
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
                all_domains=args.all_domains,
            )
            saved_path, up_cookies, tot_cookies, up_ls_keys, tot_origins = upsert_session_state(
                new_cookies=None if args.no_cookies else cookies,
                new_origins=None if args.no_local_storage else origins,
                refreshed_domains=domains,
                state_path=state_path,
                replace_all=args.replace,
            )
            if args.target != "none":
                more_mcp = ensure_mcp_configured(
                    [args.target],
                    state_path=saved_path,
                    cdp_endpoint=args.cdp_endpoint,
                    engine=effective_engine,
                )
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

    if has_fork_request:
        shell_info = (
            inspect_mcp_headless_shell(config_paths=target_mcp_config_paths(args.target))
            if args.target != "none"
            else None
        )
        result = {
            "ok": True,
            "browser": args.browser,
            "source_profile": profile_label,
            "refreshed_domains": domains,
            "forked_cookies": up_cookies,
            "total_cached_cookies": tot_cookies,
            "forked_local_storage_keys": up_ls_keys,
            "total_cached_origins": tot_origins,
            "state_file": str(saved_path),
            "hot_reload_state_file": str(hot_reload_state_path(saved_path)),
            "mcp_server": MCP_SERVER_NAME,
            "updated_mcp_configs": [str(p) for p in updated_mcp],
            "installed_skills": [str(p) for p in installed_skills],
        }
        if shell_info is not None:
            result["playwright_headless_shell"] = shell_info["playwright_headless_shell"]
            result["cdp_mcp_configs"] = shell_info["cdp_mcp_configs"]
            result["engine_modes"] = shell_info["engine_modes"]
            result["moli_binary"] = shell_info["moli_binary"]
            result["rust_mcp_binary"] = shell_info["rust_mcp_binary"]
            result["rust_mcp_version"] = shell_info["rust_mcp_version"]
            result["warning"] = shell_info["warning"]
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
                    f"Check that you are signed in to {', '.join(domains)} in {args.browser}.",
                    file=sys.stderr,
                )
            for cfg in updated_mcp:
                print(f"Wired headless MCP '{MCP_SERVER_NAME}' in {cfg}")
            for sk in installed_skills:
                print(f"Installed Agent Skill in {sk}")
            if shell_info is not None and shell_info["warning"]:
                print(f"Warning: {shell_info['warning']}", file=sys.stderr)
        return 0

    if did_work:
        shell_info = inspect_mcp_headless_shell(config_paths=target_mcp_config_paths(args.target))
        payload = {
            "ok": True,
            "action": "setup",
            "state_file": str(state_path.resolve()),
            "mcp_server": MCP_SERVER_NAME,
            "updated_mcp_configs": [str(p) for p in updated_mcp],
            "installed_skills": [str(p) for p in installed_skills],
            "playwright_headless_shell": shell_info["playwright_headless_shell"],
            "cdp_mcp_configs": shell_info["cdp_mcp_configs"],
            "engine_modes": shell_info["engine_modes"],
            "moli_binary": shell_info["moli_binary"],
            "rust_mcp_binary": shell_info["rust_mcp_binary"],
            "rust_mcp_version": shell_info["rust_mcp_version"],
            "warning": shell_info["warning"],
        }
        if args.as_json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print(f"sessfork setup complete (state path: {state_path.resolve()}).")
            for cfg in updated_mcp:
                print(f"  + Updated MCP config: {cfg}")
            for sk in installed_skills:
                print(f"  + Installed SKILL.md: {sk}")
            if shell_info["engine_modes"]:
                modes_str = ", ".join(sorted(set(shell_info["engine_modes"].values())))
                print(f"  + Active engine mode(s): {modes_str}")
            if shell_info["cdp_mcp_configs"]:
                unique_eps = sorted(set(shell_info["cdp_mcp_configs"].values()))
                print(f"  + External CDP endpoint(s): {', '.join(unique_eps)}")
            elif shell_info["playwright_headless_shell"]:
                print(f"  + Playwright headless shell: {shell_info['playwright_headless_shell']}")
            if shell_info["moli_binary"]:
                ver = f" ({shell_info['moli_version']})" if shell_info.get("moli_version") else ""
                print(f"  + Moli binary: {shell_info['moli_binary']}{ver}")
            if shell_info.get("rust_mcp_binary"):
                rver = f" ({shell_info['rust_mcp_version']})" if shell_info.get("rust_mcp_version") else ""
                print(f"  + Native Rust MCP: {shell_info['rust_mcp_binary']}{rver}")
            if not updated_mcp and not installed_skills:
                print("  = MCP config and SKILL.md were already up to date.")
            if shell_info["warning"]:
                print(f"  ! Warning: {shell_info['warning']}")
        return 0

    print(
        "Quick Start:\n"
        "  sessfork github.com              # Fork Chrome login & auto-wire MCP + Skill\n"
        "  sessfork app.spa.io --no-cookies # Fork LocalStorage JWT only (no Keychain prompt)\n"
        "  sessfork --refresh               # Re-fork all domains currently saved in state.json\n"
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


def _emit_mcp_shell_status(info: dict) -> None:
    if info.get("wired_mcp_configs"):
        print(f"Wired MCP config(s): {', '.join(info['wired_mcp_configs'])}")
    engine_modes = info.get("engine_modes") or {}
    if engine_modes:
        modes_str = ", ".join(sorted(set(engine_modes.values())))
        print(f"Active engine mode(s): {modes_str} (switch anytime via `sessfork --engine chrome|moli|dual` or `sessfork --ui`)")
    if info.get("moli_binary"):
        ver = f" ({info['moli_version']})" if info.get("moli_version") else ""
        print(f"Moli binary: {info['moli_binary']}{ver}")
    if info.get("rust_mcp_binary"):
        rver = f" ({info['rust_mcp_version']})" if info.get("rust_mcp_version") else ""
        print(f"Native Rust MCP: {info['rust_mcp_binary']}{rver}")
    cdp_configs = info.get("cdp_mcp_configs") or {}
    if cdp_configs:
        unique_eps = sorted(set(cdp_configs.values()))
        print(
            f"External CDP backend: {', '.join(unique_eps)} "
            f"(wired in {len(cdp_configs)} config(s); pass `--cdp-endpoint local` to reset)"
        )
    pinned = info.get("pinned_mcp_configs") or []
    unpinned = info.get("unpinned_mcp_configs") or []
    stale = info.get("stale_mcp_configs") or []
    total_wired = len(pinned) + len(unpinned) + len(stale)
    shell_path = info.get("playwright_headless_shell")
    if (cdp_configs or (engine_modes and all(m in ("cdp", "moli") for m in engine_modes.values()))) and total_wired == 0:
        pass
    elif info.get("auto_pin_disabled"):
        print("Playwright headless shell: auto-pinning disabled via SESSFORK_BROWSER_EXECUTABLE_PATH")
    elif shell_path:
        if total_wired > 0:
            print(
                f"Playwright headless shell: {shell_path} "
                f"(pinned in {len(pinned)}/{total_wired} wired MCP config(s))"
            )
        else:
            print(f"Playwright headless shell: {shell_path}")
    else:
        print("Playwright headless shell: not found")
    if info.get("warning"):
        print(f"Warning: {info['warning']}")


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
        _emit_mcp_shell_status(info)
        return
    print(
        f"State file: {info['path']} "
        f"({info['total_cookies']} cookies, {info['total_local_storage_keys']} LocalStorage keys "
        f"across {info['total_origins']} origin(s))"
    )
    if info.get("hot_reload_path"):
        print(f"Hot-reload path: {info['hot_reload_path']}")
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
    _emit_mcp_shell_status(info)


if __name__ == "__main__":
    sys.exit(main())
