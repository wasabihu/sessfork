#!/usr/bin/env python3
"""Reproducible 5-scenario local stress test & benchmark harness for sessfork.

Zero external network requests, zero real user Chrome data, zero OS Keychain prompts.
All fixtures live in isolated temporary directories and talk only to a local 127.0.0.1 HTTP echo server.

Scenarios:
  1. Scale & throughput: 10 / 50 / 100 origins (AES-128-CBC SQLite + LevelDB SSTable/WAL)
  2. Multi-process concurrent fork storm (8 & 16 workers -> shared state.json):
     verifies zero lost updates, zero state/mirror divergence, and zero torn JSON reads
  3. Live SQLite WAL contention: active writer hammering Cookies DB while 8 workers extract
  4. Real @playwright/mcp + chrome-headless-shell 50-origin hydration & 20-cycle hot-reload storm
  5. Real @playwright/mcp 6-agent isolatedContext concurrency & zero cross-context leak check

Usage:
  python3 scripts/stress_test.py
  python3 scripts/stress_test.py --skip-mcp
  python3 scripts/stress_test.py --output bench-report.json
"""

from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import multiprocessing as mp
import os
import platform
import select
import socketserver
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from sessfork.chrome import CHROMIUM_EPOCH_DELTA_SECONDS, extract_cookies
from sessfork.crypto import MACOS_ITERATIONS, aes_128_cbc_encrypt_pkcs7, derive_aes_key
from sessfork.localstorage import (
    K_TYPE_DELETION,
    K_TYPE_VALUE,
    LEVELDB_TABLE_MAGIC,
    LOG_FULL,
    extract_local_storage,
)
from sessfork.mcp_setup import (
    DEFAULT_PLAYWRIGHT_MCP_PKG,
    build_playwright_mcp_entry,
    find_playwright_headless_shell,
    resolve_npx_command,
)
from sessfork.moli_bridge import find_moli_binary, get_moli_version
from sessfork.state import (
    hot_reload_state_path,
    load_storage_state,
    upsert_session_state,
)


def pct(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round((p / 100.0) * (len(s) - 1)))))
    return s[idx]


def unix_to_chromium_us(ts: float) -> int:
    return int((ts + CHROMIUM_EPOCH_DELTA_SECONDS) * 1_000_000)


def encrypt_cookie(value: str, host_key: str, aes_key: bytes) -> bytes:
    prefix = hashlib.sha256(host_key.encode("utf-8")).digest()
    return b"v10" + aes_128_cbc_encrypt_pkcs7(prefix + value.encode("utf-8"), aes_key)


def encode_varint(val: int) -> bytes:
    out = bytearray()
    while val >= 0x80:
        out.append((val & 0x7F) | 0x80)
        val >>= 7
    out.append(val)
    return bytes(out)


def build_block(entries: List[Tuple[bytes, bytes]]) -> bytes:
    buf = bytearray()
    prev = b""
    for k, v in entries:
        shared = 0
        max_sh = min(len(prev), len(k))
        while shared < max_sh and prev[shared] == k[shared]:
            shared += 1
        buf.extend(_var := encode_varint(shared))
        del _var
        buf.extend(encode_varint(len(k) - shared))
        buf.extend(encode_varint(len(v)))
        buf.extend(k[shared:])
        buf.extend(v)
        prev = k
    buf.extend(struct.pack("<II", 0, 1))
    return bytes(buf)


def build_sstable(records: List[Tuple[bytes, int, int, bytes]]) -> bytes:
    data_entries = [(uk + struct.pack("<Q", (seq << 8) | vt), val) for uk, seq, vt, val in records]
    raw_data_block = build_block(data_entries)
    file_buf = bytearray(raw_data_block)
    file_buf.extend(struct.pack("<BI", 0, 0))
    meta_offset = len(file_buf)
    meta_block = build_block([])
    file_buf.extend(meta_block)
    file_buf.extend(struct.pack("<BI", 0, 0))
    index_offset = len(file_buf)
    handle = encode_varint(0) + encode_varint(len(raw_data_block))
    last_key = data_entries[-1][0] if data_entries else b"z"
    index_block = build_block([(last_key, handle)])
    file_buf.extend(index_block)
    file_buf.extend(struct.pack("<BI", 0, 0))
    footer_handles = (
        encode_varint(meta_offset)
        + encode_varint(len(meta_block))
        + encode_varint(index_offset)
        + encode_varint(len(index_block))
    )
    file_buf.extend(footer_handles.ljust(40, b"\x00") + struct.pack("<Q", LEVELDB_TABLE_MAGIC))
    return bytes(file_buf)


def build_wal_batch(base_seq: int, ops: List[Tuple[int, bytes, bytes]]) -> bytes:
    batch = bytearray(struct.pack("<QI", base_seq, len(ops)))
    for vtype, k, v in ops:
        batch.append(vtype)
        batch.extend(encode_varint(len(k)))
        batch.extend(k)
        if vtype == K_TYPE_VALUE:
            batch.extend(encode_varint(len(v)))
            batch.extend(v)
    return bytes(batch)


def build_synthetic_profile(
    root: Path,
    num_domains: int,
    cookies_per_domain: int,
    ls_per_domain: int,
    aes_key: bytes,
) -> Tuple[Path, Path, List[str]]:
    db_path = root / "Default" / "Network" / "Cookies"
    ldb_dir = root / "Default" / "Local Storage" / "leveldb"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    ldb_dir.mkdir(parents=True, exist_ok=True)

    domains = [f"svc{i:03d}.bench.test" for i in range(num_domains)]
    future_exp = unix_to_chromium_us(time.time() + 86400)

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL;")
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
    rows = []
    for d in domains:
        host = "." + d
        for c in range(cookies_per_domain):
            val = f"tok_{d}_{c}_" + ("x" * 64)
            rows.append(
                (
                    host,
                    f"ck_{c:02d}",
                    "",
                    encrypt_cookie(val, host, aes_key),
                    "/",
                    future_exp,
                    1,
                    1,
                    1,
                    1,
                )
            )
    conn.executemany("INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);", rows)
    conn.commit()
    conn.close()

    seq = 1
    sst_idx = 1
    for start in range(0, num_domains, 10):
        recs = []
        for d in domains[start : start + 10]:
            for k in range(ls_per_domain):
                uk = f"_https://{d}\x00\x01key_{k:02d}".encode("utf-8")
                uv = b"\x01" + (f"jwt_{d}_{k}_" + ("y" * 96)).encode("utf-8")
                recs.append((uk, seq, K_TYPE_VALUE, uv))
                seq += 1
        recs.sort(key=lambda r: r[0])
        (ldb_dir / f"{sst_idx:06d}.ldb").write_bytes(build_sstable(recs))
        sst_idx += 1

    wal_ops = []
    for d in domains[: min(10, num_domains)]:
        uk0 = f"_https://{d}\x00\x01key_00".encode("utf-8")
        wal_ops.append((K_TYPE_VALUE, uk0, b"\x01wal_fresh_" + d.encode("utf-8")))
        uk_del = f"_https://{d}\x00\x01stale_key".encode("utf-8")
        wal_ops.append((K_TYPE_DELETION, uk_del, b""))
    wal_batch = build_wal_batch(seq + 100, wal_ops)
    rec = struct.pack("<IHB", 0, len(wal_batch), LOG_FULL) + wal_batch
    (ldb_dir / f"{sst_idx:06d}.log").write_bytes(rec)
    return db_path, ldb_dir, domains


def run_scenario_1(aes_key: bytes) -> Dict[str, Any]:
    results = []
    for num_domains in (10, 50, 100):
        with tempfile.TemporaryDirectory(prefix="sf_s1_") as tmp:
            root = Path(tmp)
            db_path, ldb_dir, domains = build_synthetic_profile(
                root, num_domains=num_domains, cookies_per_domain=20, ls_per_domain=20, aes_key=aes_key
            )
            state_file = root / "out" / "state.json"

            ck_ms, ls_ms, wr_ms, tot_ms = [], [], [], []
            n_ck = n_ls = 0
            for _ in range(5):
                t0 = time.perf_counter()
                cookies, _ = extract_cookies(domains, db_path=db_path, aes_key=aes_key)
                t1 = time.perf_counter()
                origins = extract_local_storage(ldb_dir, domains)
                t2 = time.perf_counter()
                upsert_session_state(
                    cookies,
                    new_origins=origins,
                    refreshed_domains=domains,
                    state_path=state_file,
                    replace_all=True,
                )
                t3 = time.perf_counter()
                ck_ms.append((t1 - t0) * 1000)
                ls_ms.append((t2 - t1) * 1000)
                wr_ms.append((t3 - t2) * 1000)
                tot_ms.append((t3 - t0) * 1000)
                n_ck = len(cookies)
                n_ls = sum(len(o["localStorage"]) for o in origins)

            size_kb = round(state_file.stat().st_size / 1024, 1)
            results.append(
                {
                    "domains": num_domains,
                    "cookies": n_ck,
                    "ls_keys": n_ls,
                    "state_kb": size_kb,
                    "cookie_p50_ms": round(pct(ck_ms, 50), 2),
                    "leveldb_p50_ms": round(pct(ls_ms, 50), 2),
                    "write_p50_ms": round(pct(wr_ms, 50), 2),
                    "total_p50_ms": round(pct(tot_ms, 50), 2),
                    "total_p95_ms": round(pct(tot_ms, 95), 2),
                    "cookies_per_sec": round(n_ck / (pct(ck_ms, 50) / 1000.0), 1),
                }
            )
    return {"scenario": "1_scale_throughput", "tiers": results}


def _mp_fork_worker(args: Tuple[int, str, int]) -> Tuple[int, int, float, Optional[str]]:
    worker_id, state_path_str, rounds = args
    state_path = Path(state_path_str)
    t0 = time.perf_counter()
    try:
        for r in range(rounds):
            dom = f"agent-{worker_id:02d}-r{r:02d}.concurrent.test"
            ck = [
                {
                    "name": "sid",
                    "value": f"val_{worker_id}_{r}",
                    "domain": "." + dom,
                    "path": "/",
                    "expires": time.time() + 3600,
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                }
            ]
            orig = [
                {
                    "origin": f"https://{dom}",
                    "localStorage": [{"name": "jwt", "value": f"jwt_{worker_id}_{r}"}],
                }
            ]
            upsert_session_state(ck, new_origins=orig, refreshed_domains=[dom], state_path=state_path)
        return worker_id, rounds, (time.perf_counter() - t0) * 1000, None
    except Exception as exc:
        return worker_id, 0, (time.perf_counter() - t0) * 1000, f"{type(exc).__name__}: {exc}"


def _mp_reader_worker(state_path_str: str, stop_ev: Any, out_q: Any) -> None:
    mirror = hot_reload_state_path(Path(state_path_str))
    reads = 0
    torn = 0
    while not stop_ev.is_set():
        if mirror.is_file():
            try:
                raw = mirror.read_text(encoding="utf-8")
                json.loads(raw)
                reads += 1
            except json.JSONDecodeError:
                torn += 1
            except OSError:
                pass
        time.sleep(0.001)
    out_q.put((reads, torn))


def run_scenario_2() -> Dict[str, Any]:
    runs = []
    ctx = mp.get_context("spawn")
    for n_workers, rounds in ((8, 5), (16, 4)):
        with tempfile.TemporaryDirectory(prefix="sf_s2_") as tmp:
            state_file = Path(tmp) / "store" / "state.json"
            expected = n_workers * rounds
            stop_ev = ctx.Event()
            out_q = ctx.Queue()
            reader = ctx.Process(target=_mp_reader_worker, args=(str(state_file), stop_ev, out_q))
            reader.start()
            t0 = time.perf_counter()
            with ctx.Pool(processes=n_workers) as pool:
                worker_res = pool.map(
                    _mp_fork_worker,
                    [(i, str(state_file), rounds) for i in range(n_workers)],
                )
            wall_ms = (time.perf_counter() - t0) * 1000
            stop_ev.set()
            reader.join(timeout=5)
            reads, torn = out_q.get(timeout=5)

            errors = [e for (_, _, _, e) in worker_res if e]
            main_data = load_storage_state(state_file)
            mirror_path = hot_reload_state_path(state_file)
            mirror_data = load_storage_state(mirror_path)
            present_cookies = len(main_data["cookies"])
            present_origins = len(main_data["origins"])
            lost_cookies = expected - present_cookies
            lost_origins = expected - present_origins
            mirror_diverged = main_data != mirror_data

            runs.append(
                {
                    "workers": n_workers,
                    "rounds_each": rounds,
                    "expected_domains": expected,
                    "present_cookies": present_cookies,
                    "lost_cookies": lost_cookies,
                    "lost_update_pct": round((lost_cookies / expected) * 100, 1),
                    "present_origins": present_origins,
                    "lost_origins": lost_origins,
                    "mirror_diverged": mirror_diverged,
                    "mirror_cookies": len(mirror_data["cookies"]),
                    "concurrent_reader_reads": reads,
                    "torn_json_reads": torn,
                    "worker_errors": errors[:5],
                    "wall_ms": round(wall_ms, 1),
                }
            )
    return {"scenario": "2_concurrent_fork_storm", "runs": runs}


def _wal_writer_proc(db_path_str: str, aes_key: bytes, stop_ev: Any, out_q: Any) -> None:
    conn = sqlite3.connect(db_path_str, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    writes = 0
    checkpoints = 0
    future_exp = unix_to_chromium_us(time.time() + 3600)
    try:
        while not stop_ev.is_set():
            val = f"live_wal_val_{writes}"
            blob = encrypt_cookie(val, ".waltest.local", aes_key)
            conn.execute(
                "UPDATE cookies SET encrypted_value = ?, expires_utc = ? WHERE host_key = '.waltest.local' AND name = 'sid'",
                (blob, future_exp),
            )
            conn.execute(
                "INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (".waltest.local", f"ephem_{writes % 50}", "", blob, "/", future_exp, 1, 1, 1, 1),
            )
            conn.commit()
            writes += 1
            if writes % 25 == 0:
                conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
                checkpoints += 1
    finally:
        conn.close()
        out_q.put((writes, checkpoints))


def _wal_reader_worker(args: Tuple[str, bytes, int]) -> Tuple[int, int, List[float], List[str]]:
    db_path_str, aes_key, iters = args
    db_path = Path(db_path_str)
    ok = 0
    fail = 0
    lat = []
    errs = []
    for _ in range(iters):
        t0 = time.perf_counter()
        try:
            cks, _ = extract_cookies(["waltest.local"], db_path=db_path, aes_key=aes_key)
            if not any(c["name"] == "sid" for c in cks):
                fail += 1
                errs.append("missing_sid_cookie")
            else:
                ok += 1
        except Exception as exc:
            fail += 1
            errs.append(f"{type(exc).__name__}: {exc}")
        lat.append((time.perf_counter() - t0) * 1000)
    return ok, fail, lat, errs


def run_scenario_3(aes_key: bytes) -> Dict[str, Any]:
    ctx = mp.get_context("spawn")
    with tempfile.TemporaryDirectory(prefix="sf_s3_") as tmp:
        db_path = Path(tmp) / "Default" / "Network" / "Cookies"
        db_path.parent.mkdir(parents=True)
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL;")
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
        future_exp = unix_to_chromium_us(time.time() + 3600)
        blob = encrypt_cookie("initial_val", ".waltest.local", aes_key)
        conn.execute(
            "INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
            (".waltest.local", "sid", "", blob, "/", future_exp, 1, 1, 1, 1),
        )
        conn.commit()
        conn.close()

        stop_ev = ctx.Event()
        out_q = ctx.Queue()
        writer = ctx.Process(target=_wal_writer_proc, args=(str(db_path), aes_key, stop_ev, out_q))
        writer.start()
        time.sleep(0.15)

        with ctx.Pool(processes=8) as pool:
            res = pool.map(_wal_reader_worker, [(str(db_path), aes_key, 25) for _ in range(8)])

        stop_ev.set()
        writer.join(timeout=5)
        writes, checkpoints = out_q.get(timeout=5)

        total_ok = sum(r[0] for r in res)
        total_fail = sum(r[1] for r in res)
        all_lat = [x for r in res for x in r[2]]
        all_errs = [e for r in res for e in r[3]]
        return {
            "scenario": "3_live_sqlite_wal_contention",
            "writer_commits": writes,
            "writer_checkpoints": checkpoints,
            "reader_workers": 8,
            "total_extractions": total_ok + total_fail,
            "ok": total_ok,
            "failed": total_fail,
            "error_rate_pct": round((total_fail / max(1, total_ok + total_fail)) * 100, 2),
            "sample_errors": all_errs[:5],
            "p50_ms": round(pct(all_lat, 50), 2),
            "p95_ms": round(pct(all_lat, 95), 2),
            "p99_ms": round(pct(all_lat, 99), 2),
        }


class LocalAuthHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        cookie_hdr = self.headers.get("Cookie", "")
        html = f"""<!doctype html><html><head><title>AuthEcho</title></head><body>
<pre id="out"></pre>
<script>
const data = {{
  cookie: {json.dumps(cookie_hdr)},
  lsToken: window.localStorage.getItem('session_token') || '',
  lsAgent: window.localStorage.getItem('agent_id') || ''
}};
document.getElementById('out').textContent = JSON.stringify(data);
document.title = 'ECHO:' + data.cookie + '|' + data.lsToken + '|' + data.lsAgent;
</script>
</body></html>"""
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def get_process_tree_rss_mb(root_pid: int) -> float:
    try:
        out = subprocess.check_output(["ps", "-eo", "pid,ppid,rss"], text=True, timeout=5)
    except Exception:
        return 0.0
    children: Dict[int, List[int]] = {}
    rss_kb: Dict[int, int] = {}
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) != 3:
            continue
        p, pp, r = int(parts[0]), int(parts[1]), int(parts[2])
        children.setdefault(pp, []).append(p)
        rss_kb[p] = r
    total = 0
    stack = [root_pid]
    seen = set()
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        total += rss_kb.get(cur, 0)
        stack.extend(children.get(cur, []))
    return round(total / 1024.0, 1)


class MCPStdioClient:
    def __init__(self, cmd: str, args: List[str], env: Dict[str, str]):
        full_env = dict(os.environ)
        full_env.update(env)
        self.proc = subprocess.Popen(
            [cmd] + args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=full_env,
            bufsize=0,
        )
        self._id = 0

    def _send(self, obj: Dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def _recv(self, expected_id: int, timeout: float = 45.0) -> Dict[str, Any]:
        assert self.proc.stdout is not None
        deadline = time.time() + timeout
        buf = b""
        while time.time() < deadline:
            remaining = max(0.05, deadline - time.time())
            ready, _, _ = select.select([self.proc.stdout], [], [], remaining)
            if not ready:
                continue
            chunk = os.read(self.proc.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError("MCP server closed stdout unexpectedly")
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line_s = line.decode("utf-8", errors="replace").strip()
                if not line_s or not line_s.startswith("{"):
                    continue
                msg = json.loads(line_s)
                if msg.get("id") == expected_id:
                    return msg
        raise TimeoutError(f"MCP request {expected_id} timed out after {timeout}s")

    def initialize(self) -> Dict[str, Any]:
        self._id += 1
        req_id = self._id
        self._send(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "sessfork-stress", "version": "1.0"},
                },
            }
        )
        resp = self._recv(req_id, timeout=60.0)
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        return resp

    def call_tool(self, name: str, arguments: Dict[str, Any], timeout: float = 30.0) -> Dict[str, Any]:
        self._id += 1
        req_id = self._id
        self._send(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        return self._recv(req_id, timeout=timeout)

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def _extract_mcp_text(resp: Dict[str, Any]) -> str:
    res = resp.get("result", {})
    parts = []
    for item in res.get("content", []):
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text", "")))
    return "\n".join(parts)


def run_scenarios_4_and_5() -> Tuple[Dict[str, Any], Dict[str, Any]]:
    server = socketserver.TCPServer(("127.0.0.1", 0), LocalAuthHandler)
    port = server.server_address[1]
    srv_thread = threading.Thread(target=server.serve_forever, daemon=True)
    srv_thread.start()
    origin = f"http://127.0.0.1:{port}"
    url = f"{origin}/whoami"

    with tempfile.TemporaryDirectory(prefix="sf_s45_") as tmp:
        tmp_dir = Path(tmp)
        state_file = tmp_dir / "state.json"

        future_exp = time.time() + 7200
        base_cookies = [
            {
                "name": "sid",
                "value": "v0_cold_start",
                "domain": "127.0.0.1",
                "path": "/",
                "expires": future_exp,
                "httpOnly": False,
                "secure": False,
                "sameSite": "Lax",
            }
        ]
        base_origins = [
            {
                "origin": origin,
                "localStorage": [
                    {"name": "session_token", "value": "ls_v0_cold"},
                    {"name": "agent_id", "value": "cold"},
                ],
            }
        ]
        for i in range(49):
            dom = f"origin-{i:02d}.bench.test"
            for c in range(20):
                base_cookies.append(
                    {
                        "name": f"ck_{c}",
                        "value": f"val_{i}_{c}",
                        "domain": "." + dom,
                        "path": "/",
                        "expires": future_exp,
                        "httpOnly": True,
                        "secure": True,
                        "sameSite": "Lax",
                    }
                )
            base_origins.append(
                {
                    "origin": f"https://{dom}",
                    "localStorage": [{"name": f"k_{j}", "value": f"v_{i}_{j}"} for j in range(20)],
                }
            )
        upsert_session_state(base_cookies, new_origins=base_origins, state_path=state_file, replace_all=True)
        mirror = hot_reload_state_path(state_file)

        entry = build_playwright_mcp_entry(state_path=state_file)
        client = MCPStdioClient(entry["command"], entry["args"], entry["env"])
        s4: Dict[str, Any] = {"scenario": "4_playwright_mcp_hot_reload_50_origins"}
        s5: Dict[str, Any] = {"scenario": "5_playwright_mcp_isolated_contexts"}

        try:
            t_init0 = time.perf_counter()
            client.initialize()
            t_init_ms = (time.perf_counter() - t_init0) * 1000

            t_nav0 = time.perf_counter()
            nav_resp = client.call_tool("browser_navigate", {"url": url}, timeout=45.0)
            cold_nav_ms = (time.perf_counter() - t_nav0) * 1000
            nav_txt = _extract_mcp_text(nav_resp)
            cold_ok = ("sid=v0_cold_start" in nav_txt) and ("ls_v0_cold" in nav_txt)
            rss_after_cold = get_process_tree_rss_mb(client.proc.pid)

            reload_lat: List[float] = []
            nav_lat: List[float] = []
            cycle_ok = 0
            cycle_fail = 0
            cycle_errs: List[str] = []

            for i in range(1, 21):
                ck_i = [
                    {
                        "name": "sid",
                        "value": f"tok_v{i}",
                        "domain": "127.0.0.1",
                        "path": "/",
                        "expires": future_exp,
                        "httpOnly": False,
                        "secure": False,
                        "sameSite": "Lax",
                    }
                ]
                orig_i = [
                    {
                        "origin": origin,
                        "localStorage": [
                            {"name": "session_token", "value": f"ls_v{i}"},
                            {"name": "agent_id", "value": f"cycle_{i}"},
                        ],
                    }
                ]
                upsert_session_state(
                    ck_i, new_origins=orig_i, refreshed_domains=["127.0.0.1"], state_path=state_file
                )
                tr0 = time.perf_counter()
                r1 = client.call_tool("browser_set_storage_state", {"filename": str(mirror)}, timeout=30.0)
                tr1 = time.perf_counter()
                r2 = client.call_tool("browser_navigate", {"url": f"{url}?c={i}"}, timeout=30.0)
                tr2 = time.perf_counter()
                reload_lat.append((tr1 - tr0) * 1000)
                nav_lat.append((tr2 - tr1) * 1000)
                txt1 = _extract_mcp_text(r1)
                txt2 = _extract_mcp_text(r2)
                if r1.get("result", {}).get("isError") or r2.get("result", {}).get("isError"):
                    cycle_fail += 1
                    cycle_errs.append(f"c{i}: {txt1[:120]} | {txt2[:120]}")
                elif (f"sid=tok_v{i}" in txt2) and (f"ls_v{i}" in txt2):
                    cycle_ok += 1
                else:
                    cycle_fail += 1
                    cycle_errs.append(f"c{i}_stale_or_missing: {txt2[:160]}")

            rss_after_20 = get_process_tree_rss_mb(client.proc.pid)
            shell_path = find_playwright_headless_shell()
            s4.update(
                {
                    "origins_in_state": len(base_origins),
                    "cookies_in_state": len(base_cookies),
                    "headless_shell_name": shell_path.name if shell_path else None,
                    "mcp_init_ms": round(t_init_ms, 1),
                    "cold_50_origin_hydrate_ms": round(cold_nav_ms, 1),
                    "cold_start_verified": cold_ok,
                    "hot_reload_cycles": 20,
                    "cycles_passed": cycle_ok,
                    "cycles_failed": cycle_fail,
                    "reload_p50_ms": round(pct(reload_lat, 50), 1),
                    "reload_p95_ms": round(pct(reload_lat, 95), 1),
                    "nav_p50_ms": round(pct(nav_lat, 50), 1),
                    "nav_p95_ms": round(pct(nav_lat, 95), 1),
                    "rss_after_cold_mb": rss_after_cold,
                    "rss_after_20_cycles_mb": rss_after_20,
                    "rss_delta_mb": round(rss_after_20 - rss_after_cold, 1),
                    "sample_errors": cycle_errs[:3],
                }
            )

            n_ctx = 6
            ctx_tab_indices: Dict[int, int] = {}
            setup_ok = 0
            leaks = 0
            iso_errs: List[str] = []
            t_s5 = time.perf_counter()

            for idx in range(n_ctx):
                agent_state = mirror.parent / f".agent-ctx-{idx}.json"
                agent_payload = {
                    "cookies": [
                        {
                            "name": "sid",
                            "value": f"user_{idx}_cookie",
                            "domain": "127.0.0.1",
                            "path": "/",
                            "expires": future_exp,
                            "httpOnly": False,
                            "secure": False,
                            "sameSite": "Lax",
                        }
                    ],
                    "origins": [
                        {
                            "origin": origin,
                            "localStorage": [
                                {"name": "session_token", "value": f"user_{idx}_jwt"},
                                {"name": "agent_id", "value": f"agent_{idx}"},
                            ],
                        }
                    ],
                }
                agent_state.write_text(json.dumps(agent_payload), encoding="utf-8")
                os.chmod(agent_state, 0o600)

                client.call_tool("browser_tabs", {"action": "new", "isolatedContext": f"agent-ctx-{idx}"})
                ctx_tab_indices[idx] = idx + 1
                client.call_tool("browser_set_storage_state", {"filename": str(agent_state)})
                r_nav = client.call_tool("browser_navigate", {"url": f"{url}?init={idx}"})
                nav_t = _extract_mcp_text(r_nav)
                if (f"sid=user_{idx}_cookie" in nav_t) and (f"user_{idx}_jwt" in nav_t):
                    setup_ok += 1
                else:
                    iso_errs.append(f"setup_{idx}: {nav_t[:140]}")

            verify_checks = 0
            verify_passed = 0
            for idx in [5, 0, 3, 1, 4, 2]:
                client.call_tool("browser_tabs", {"action": "select", "index": ctx_tab_indices[idx]})
                r_chk = client.call_tool("browser_navigate", {"url": f"{url}?verify={idx}"})
                chk_txt = _extract_mcp_text(r_chk)
                page_title_line = ""
                for ln in chk_txt.splitlines():
                    if ln.strip().startswith("- Page Title:"):
                        page_title_line = ln.strip()
                        break
                verify_checks += 1
                expected_ck = f"sid=user_{idx}_cookie"
                expected_ls = f"user_{idx}_jwt"
                has_other = any(
                    (f"user_{j}_cookie" in page_title_line or f"user_{j}_jwt" in page_title_line)
                    for j in range(n_ctx)
                    if j != idx
                )
                if expected_ck in page_title_line and expected_ls in page_title_line and not has_other:
                    verify_passed += 1
                else:
                    leaks += 1
                    iso_errs.append(f"verify_{idx}: {page_title_line or chk_txt[:160]}")

            rss_after_6ctx = get_process_tree_rss_mb(client.proc.pid)
            s5.update(
                {
                    "isolated_contexts": n_ctx,
                    "setup_verified": setup_ok,
                    "interleaved_checks": verify_checks,
                    "interleaved_passed": verify_passed,
                    "cross_context_leaks": leaks,
                    "rss_with_6_contexts_mb": rss_after_6ctx,
                    "total_s5_ms": round((time.perf_counter() - t_s5) * 1000, 1),
                    "sample_errors": iso_errs[:3],
                }
            )
        finally:
            client.close()
            server.shutdown()
            server.server_close()

    return s4, s5


def run_scenario_6_moli() -> Dict[str, Any]:
    """Benchmark lexmount/moli via managed CDP on the exact same 6x isolatedContext workload."""
    import socket as _sock
    import urllib.request as _urlreq

    moli_bin = find_moli_binary()
    if moli_bin is None:
        return {"scenario": "6_moli_isolated_contexts", "skipped": True, "reason": "moli binary not installed"}

    server = socketserver.TCPServer(("127.0.0.1", 0), LocalAuthHandler)
    http_port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{http_port}"
    url = f"{origin}/whoami"

    with _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        cdp_port = int(s.getsockname()[1])
    cdp_url = f"http://127.0.0.1:{cdp_port}"

    with tempfile.TemporaryDirectory(prefix="sf_s6_moli_") as tmp:
        out_dir = Path(tmp) / "playwright-output"
        out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        t_m0 = time.perf_counter()
        moli_proc = subprocess.Popen(
            [str(moli_bin), "serve", "--host", "127.0.0.1", "--port", str(cdp_port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        client: Optional[MCPStdioClient] = None
        try:
            for _ in range(200):
                try:
                    _urlreq.urlopen(f"{cdp_url}/json/version", timeout=0.5).read()
                    break
                except Exception:
                    time.sleep(0.025)
            cdp_ready_ms = round((time.perf_counter() - t_m0) * 1000, 1)
            idle_rss_mb = get_process_tree_rss_mb(moli_proc.pid)

            npx = resolve_npx_command()
            client = MCPStdioClient(
                npx,
                [
                    "-y",
                    DEFAULT_PLAYWRIGHT_MCP_PKG,
                    f"--cdp-endpoint={cdp_url}",
                    "--caps=storage",
                    f"--output-dir={out_dir}",
                ],
                {},
            )
            client.initialize()

            n_ctx = 6
            future_exp = time.time() + 7200
            t_s6 = time.perf_counter()
            setup_ok = 0
            for idx in range(n_ctx):
                st = out_dir / f".moli-ctx-{idx}.json"
                st.write_text(
                    json.dumps(
                        {
                            "cookies": [
                                {
                                    "name": "sid",
                                    "value": f"user_{idx}_cookie",
                                    "domain": "127.0.0.1",
                                    "path": "/",
                                    "expires": future_exp,
                                    "httpOnly": False,
                                    "secure": False,
                                    "sameSite": "Lax",
                                }
                            ],
                            "origins": [
                                {
                                    "origin": origin,
                                    "localStorage": [
                                        {"name": "session_token", "value": f"user_{idx}_jwt"},
                                        {"name": "agent_id", "value": f"agent_{idx}"},
                                    ],
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                os.chmod(st, 0o600)
                client.call_tool("browser_tabs", {"action": "new", "isolatedContext": f"moli-ctx-{idx}"})
                client.call_tool("browser_set_storage_state", {"filename": str(st)})
                nav_t = _extract_mcp_text(client.call_tool("browser_navigate", {"url": f"{url}?init={idx}"}))
                if f"sid=user_{idx}_cookie" in nav_t and f"user_{idx}_jwt" in nav_t:
                    setup_ok += 1

            verify_passed = 0
            leaks = 0
            for idx in [5, 0, 3, 1, 4, 2]:
                client.call_tool("browser_tabs", {"action": "select", "index": idx + 1})
                chk_txt = _extract_mcp_text(client.call_tool("browser_navigate", {"url": f"{url}?verify={idx}"}))
                tl = next((ln.strip() for ln in chk_txt.splitlines() if ln.strip().startswith("- Page Title:")), "")
                has_other = any(
                    (f"user_{j}_cookie" in tl or f"user_{j}_jwt" in tl)
                    for j in range(n_ctx)
                    if j != idx
                )
                if f"sid=user_{idx}_cookie" in tl and f"user_{idx}_jwt" in tl and not has_other:
                    verify_passed += 1
                else:
                    leaks += 1

            moli_rss = get_process_tree_rss_mb(moli_proc.pid)
            node_rss = get_process_tree_rss_mb(client.proc.pid)
            return {
                "scenario": "6_moli_isolated_contexts",
                "moli_version": get_moli_version(moli_bin),
                "cdp_ready_ms": cdp_ready_ms,
                "idle_rss_mb": idle_rss_mb,
                "isolated_contexts": n_ctx,
                "setup_verified": setup_ok,
                "interleaved_passed": verify_passed,
                "cross_context_leaks": leaks,
                "moli_rss_mb": moli_rss,
                "node_mcp_rss_mb": node_rss,
                "total_rss_mb": round(moli_rss + node_rss, 1),
                "total_s6_ms": round((time.perf_counter() - t_s6) * 1000, 1),
            }
        finally:
            if client is not None:
                client.close()
            if moli_proc.poll() is None:
                moli_proc.terminate()
                try:
                    moli_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    moli_proc.kill()
            server.shutdown()
            server.server_close()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run local 5-scenario (+ optional Moli) stress benchmarks for sessfork.")
    parser.add_argument("--skip-mcp", action="store_true", help="Skip Scenarios 4-6 (@playwright/mcp E2E).")
    parser.add_argument("--include-moli", action="store_true", help="Also run Scenario 6 against local `moli` binary.")
    parser.add_argument("--output", type=Path, default=None, help="Optional JSON report output path.")
    args = parser.parse_args(argv)

    aes_key = derive_aes_key(b"sessfork-local-stress-test-key", MACOS_ITERATIONS)
    report: Dict[str, Any] = {
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "scenarios": [],
    }

    print("[1/5] Running Scenario 1: Scale & Throughput (10 / 50 / 100 origins)...", file=sys.stderr, flush=True)
    report["scenarios"].append(run_scenario_1(aes_key))

    print("[2/5] Running Scenario 2: Concurrent Multi-Process Fork Storm (8 & 16 workers)...", file=sys.stderr, flush=True)
    report["scenarios"].append(run_scenario_2())

    print("[3/5] Running Scenario 3: Live SQLite WAL Contention (writer + 8 readers x 25)...", file=sys.stderr, flush=True)
    report["scenarios"].append(run_scenario_3(aes_key))

    if not args.skip_mcp:
        print("[4-5/5] Running Scenarios 4 & 5: Real @playwright/mcp + chrome-headless-shell E2E...", file=sys.stderr, flush=True)
        s4, s5 = run_scenarios_4_and_5()
        report["scenarios"].append(s4)
        report["scenarios"].append(s5)
        if args.include_moli:
            print("[6/6] Running Scenario 6: Real @playwright/mcp + lexmount/moli 6x isolatedContext...", file=sys.stderr, flush=True)
            report["scenarios"].append(run_scenario_6_moli())

    payload = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.output is not None:
        args.output.write_text(payload, encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
