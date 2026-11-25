"""Zero-dependency managed Moli CDP bridge for `@playwright/mcp`.

When an AI client starts `sessfork --mcp-bridge moli`, this module:
1. Picks an ephemeral free loopback port on `127.0.0.1` (zero port 9222 collisions).
2. Starts a private `moli serve --host 127.0.0.1 --port <port>` child process (~68 ms ready).
3. Launches `@playwright/mcp --cdp-endpoint=http://127.0.0.1:<port> --caps=storage --output-dir=...`
   inheriting stdio directly so MCP JSON-RPC traffic passes through with zero overhead.
4. Always terminates the private `moli serve` child cleanly when MCP exits or receives SIGTERM/SIGINT.
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Optional

from sessfork.chrome import BrowserProfileError, real_user_home
from sessfork.state import DEFAULT_STATE_PATH, PLAYWRIGHT_OUTPUT_DIRNAME

MOLI_PATH_ENV = "SESSFORK_MOLI_PATH"
MOLI_LAYOUT_ENV = "SESSFORK_MOLI_LAYOUT"
RUST_MCP_PATH_ENV = "SESSFORK_RUST_MCP_PATH"
MOLI_RUNTIME_ENV = "SESSFORK_MOLI_RUNTIME"
MOLI_INSTALL_CMD = (
    "curl --proto '=https' --tlsv1.2 -fsSL "
    "https://github.com/lexmount/moli/releases/latest/download/moli-installer.sh | sh"
)


def find_rust_mcp_binary(
    override_path: Optional[Path] = None,
    home: Optional[Path] = None,
) -> Optional[Path]:
    """Locate the optional native Rust `sessfork-mcp` binary (zero-Node CDP pipe MCP)."""
    if os.environ.get(MOLI_RUNTIME_ENV, "").strip().lower() in ("playwright", "node"):
        return None
    if override_path is not None:
        cand = override_path.expanduser()
        return cand.resolve() if (cand.is_file() and os.access(cand, os.X_OK)) else None

    env_override = os.environ.get(RUST_MCP_PATH_ENV, "").strip()
    if env_override:
        if env_override.lower() in ("none", "off", "disabled"):
            return None
        cand = Path(env_override).expanduser()
        return cand.resolve() if (cand.is_file() and os.access(cand, os.X_OK)) else None

    # Honor explicit home or isolated SESSFORK_HOME sandboxes without leaking host ~/.local/bin.
    if home is not None:
        cand = home.expanduser() / ".local" / "bin" / "sessfork-mcp"
        return cand.resolve() if (cand.is_file() and os.access(cand, os.X_OK)) else None

    sessfork_home = os.environ.get("SESSFORK_HOME", "").strip()
    if sessfork_home:
        cand = Path(sessfork_home).expanduser() / ".local" / "bin" / "sessfork-mcp"
        return cand.resolve() if (cand.is_file() and os.access(cand, os.X_OK)) else None

    which_hit = shutil.which("sessfork-mcp")
    if which_hit:
        cand = Path(which_hit)
        if cand.is_file() and os.access(cand, os.X_OK):
            return cand.resolve()

    real_home = real_user_home()
    repo_release = Path(__file__).resolve().parents[2] / "rust" / "sessfork-mcp" / "target" / "release" / "sessfork-mcp"
    candidates = [
        real_home / ".local" / "bin" / "sessfork-mcp",
        Path.home().expanduser() / ".local" / "bin" / "sessfork-mcp",
        Path("/opt/homebrew/bin/sessfork-mcp"),
        Path("/usr/local/bin/sessfork-mcp"),
        repo_release,
    ]
    seen: set[Path] = set()
    for cand in candidates:
        if cand in seen:
            continue
        seen.add(cand)
        if cand.is_file() and os.access(cand, os.X_OK):
            return cand.resolve()
    return None


def get_rust_mcp_version(rust_mcp_bin: Optional[Path] = None) -> Optional[str]:
    """Return `sessfork-mcp --version` output string if installed, else None."""
    binary = rust_mcp_bin or find_rust_mcp_binary()
    if binary is None:
        return None
    try:
        proc = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if proc.returncode == 0:
            return proc.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None
    return None


def find_moli_binary(override_path: Optional[Path] = None) -> Optional[Path]:
    """Locate the prebuilt `moli` executable on this machine."""
    if override_path is not None:
        cand = override_path.expanduser()
        return cand.resolve() if (cand.is_file() and os.access(cand, os.X_OK)) else None

    env_override = os.environ.get(MOLI_PATH_ENV, "").strip()
    if env_override:
        cand = Path(env_override).expanduser()
        return cand.resolve() if (cand.is_file() and os.access(cand, os.X_OK)) else None

    which_hit = shutil.which("moli")
    if which_hit:
        cand = Path(which_hit)
        if cand.is_file() and os.access(cand, os.X_OK):
            return cand.resolve()

    real_home = real_user_home()
    candidates = [
        real_home / ".local" / "bin" / "moli",
        Path.home().expanduser() / ".local" / "bin" / "moli",
        Path("/opt/homebrew/bin/moli"),
        Path("/usr/local/bin/moli"),
    ]
    seen: set[Path] = set()
    for cand in candidates:
        if cand in seen:
            continue
        seen.add(cand)
        if cand.is_file() and os.access(cand, os.X_OK):
            return cand.resolve()
    return None


def get_moli_version(moli_bin: Optional[Path] = None) -> Optional[str]:
    """Return `moli --version` output string if Moli is installed, else None."""
    binary = moli_bin or find_moli_binary()
    if binary is None:
        return None
    try:
        proc = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if proc.returncode == 0:
            return proc.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None
    return None


def _pick_free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_cdp_ready(port: int, proc: subprocess.Popen[bytes], timeout_sec: float = 10.0) -> bool:
    url = f"http://127.0.0.1:{port}/json/version"
    # Bypass HTTP_PROXY/ALL_PROXY so local loopback readiness checks are never routed externally.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with opener.open(url, timeout=0.5) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.025)
    return False


def _terminate_child(proc: Optional[subprocess.Popen[bytes]], grace_sec: float = 3.0) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=grace_sec)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=2.0)
        except OSError:
            pass
    except OSError:
        pass


def run_managed_moli_mcp(
    state_path: Path = DEFAULT_STATE_PATH,
    moli_bin: Optional[Path] = None,
    npx_cmd: Optional[str] = None,
    mcp_pkg: Optional[str] = None,
) -> int:
    """Spawn a private ephemeral `moli serve` and attach `@playwright/mcp` over stdio."""
    from sessfork.mcp_setup import DEFAULT_PLAYWRIGHT_MCP_PKG, resolve_npx_command

    binary = find_moli_binary(moli_bin)
    if binary is None:
        raise BrowserProfileError(
            "Moli binary not found. Install it first with:\n"
            f"  {MOLI_INSTALL_CMD}\n"
            f"Or set {MOLI_PATH_ENV}=/path/to/moli."
        )

    raw_state = state_path.expanduser()
    if raw_state.is_symlink() or raw_state.parent.is_symlink():
        raise BrowserProfileError(f"Refusing to use symlinked state path: {raw_state}")
    resolved_state = raw_state.parent.resolve() / raw_state.name
    output_dir = resolved_state.parent / PLAYWRIGHT_OUTPUT_DIRNAME
    if output_dir.is_symlink():
        raise BrowserProfileError(f"Refusing to use symlinked output directory: {output_dir}")
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(output_dir, 0o700)

    port = _pick_free_loopback_port()
    print(
        "sessfork warning: running legacy TCP CDP fallback on 127.0.0.1 "
        "(install native Rust `sessfork-mcp` for zero-port anonymous pipe isolation on multi-user hosts).",
        file=sys.stderr,
        flush=True,
    )
    moli_cmd = [str(binary), "serve", "--host", "127.0.0.1", "--port", str(port)]
    if os.environ.get(MOLI_LAYOUT_ENV, "").strip().lower() in ("1", "true", "yes"):
        moli_cmd.append("--layout")

    # Never let Moli emit anything onto stdout — MCP JSON-RPC owns stdout exclusively.
    moli_proc = subprocess.Popen(
        moli_cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    mcp_proc: Optional[subprocess.Popen[bytes]] = None

    def _handle_signal(signum: int, _frame: object) -> None:
        _terminate_child(mcp_proc)
        _terminate_child(moli_proc)
        raise SystemExit(128 + signum)

    prev_sigterm = signal.signal(signal.SIGTERM, _handle_signal)
    prev_sigint = signal.signal(signal.SIGINT, _handle_signal)

    try:
        if not _wait_for_cdp_ready(port, moli_proc, timeout_sec=10.0):
            raise BrowserProfileError(
                f"Managed `moli serve` failed to become ready on 127.0.0.1:{port}."
            )

        npx = npx_cmd or resolve_npx_command()
        pkg = (
            mcp_pkg
            or os.environ.get("SESSFORK_PLAYWRIGHT_MCP_PKG", DEFAULT_PLAYWRIGHT_MCP_PKG).strip()
            or DEFAULT_PLAYWRIGHT_MCP_PKG
        )
        mcp_cmd = [
            npx,
            "-y",
            pkg,
            f"--cdp-endpoint=http://127.0.0.1:{port}",
            "--caps=storage",
            f"--output-dir={output_dir}",
        ]
        mcp_proc = subprocess.Popen(mcp_cmd)
        return int(mcp_proc.wait())
    finally:
        signal.signal(signal.SIGTERM, prev_sigterm)
        signal.signal(signal.SIGINT, prev_sigint)
        _terminate_child(mcp_proc)
        _terminate_child(moli_proc)
