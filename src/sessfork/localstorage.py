"""Zero-dependency Chromium LevelDB LocalStorage reader (SSTable + WAL + Snappy).

Implements a pure-Python standard-library reader for `<Profile>/Local Storage/leveldb`:
- Copies `.ldb`, `.sst`, and `.log` files into an isolated `0700` temp directory (lock-free).
- Decompresses Snappy-encoded LevelDB blocks in pure Python.
- Reconciles MVCC sequence numbers across SSTables and Write-Ahead Logs, honoring
  `kTypeDeletion` tombstones (`localStorage.removeItem` / `clear`) so stale tokens
  are never resurrected.
- Decodes Chromium's encoding-prefixed strings (`0x00` UTF-16LE, `0x01` Latin-1)
  into Playwright's `storageState["origins"]` format.
"""

from __future__ import annotations

import os
import shutil
import stat
import struct
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

LEVELDB_TABLE_MAGIC = 0xDB4775248B80FB57
LEVELDB_FOOTER_SIZE = 48
LEVELDB_BLOCK_TRAILER_SIZE = 5
LEVELDB_LOG_BLOCK_SIZE = 32768
LEVELDB_LOG_HEADER_SIZE = 7

# LevelDB ValueType
K_TYPE_DELETION = 0
K_TYPE_VALUE = 1

# LevelDB WAL RecordType
LOG_FULL = 1
LOG_FIRST = 2
LOG_MIDDLE = 3
LOG_LAST = 4

OriginEntry = Dict[str, Any]


class LevelDBFormatError(RuntimeError):
    """Raised when a LevelDB block or Snappy stream is malformed."""


def read_varint(buf: bytes, pos: int) -> Tuple[int, int]:
    """Read an unsigned base-128 little-endian varint from `buf` starting at `pos`."""
    result = 0
    shift = 0
    limit = len(buf)
    while pos < limit and shift <= 63:
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if (byte & 0x80) == 0:
            return result, pos
        shift += 7
    raise LevelDBFormatError("Truncated or overflowed varint in LevelDB stream")


def snappy_decompress(data: bytes) -> bytes:
    """Pure-Python Snappy block decompressor per Google Snappy format specification."""
    if not data:
        return b""
    expected_len, pos = read_varint(data, 0)
    if expected_len == 0:
        return b""

    out = bytearray()
    limit = len(data)

    while pos < limit:
        tag = data[pos]
        pos += 1
        elem_type = tag & 0x03

        if elem_type == 0:  # Literal
            len_minus_1 = tag >> 2
            if len_minus_1 < 60:
                lit_len = len_minus_1 + 1
            else:
                extra = len_minus_1 - 59  # 1..4 bytes
                if pos + extra > limit:
                    raise LevelDBFormatError("Truncated Snappy literal length")
                lit_len = int.from_bytes(data[pos : pos + extra], "little") + 1
                pos += extra
            if pos + lit_len > limit:
                raise LevelDBFormatError("Truncated Snappy literal payload")
            out.extend(data[pos : pos + lit_len])
            pos += lit_len
        else:
            if elem_type == 1:  # Copy with 1-byte offset (len 4..11, offset 1..2047)
                if pos >= limit:
                    raise LevelDBFormatError("Truncated Snappy 1-byte copy")
                length = ((tag >> 2) & 0x07) + 4
                offset = ((tag & 0xE0) << 3) | data[pos]
                pos += 1
            elif elem_type == 2:  # Copy with 2-byte LE offset (len 1..64)
                if pos + 2 > limit:
                    raise LevelDBFormatError("Truncated Snappy 2-byte copy")
                length = (tag >> 2) + 1
                offset = int.from_bytes(data[pos : pos + 2], "little")
                pos += 2
            else:  # elem_type == 3: Copy with 4-byte LE offset (len 1..64)
                if pos + 4 > limit:
                    raise LevelDBFormatError("Truncated Snappy 4-byte copy")
                length = (tag >> 2) + 1
                offset = int.from_bytes(data[pos : pos + 4], "little")
                pos += 4

            if offset <= 0 or offset > len(out):
                raise LevelDBFormatError(f"Invalid Snappy backreference offset {offset}")

            if offset >= length:
                start = len(out) - offset
                out.extend(out[start : start + length])
            else:
                # Overlapping RLE backreference
                for _ in range(length):
                    out.append(out[-offset])

    if len(out) != expected_len:
        raise LevelDBFormatError(
            f"Snappy uncompressed size mismatch: got {len(out)}, expected {expected_len}"
        )
    return bytes(out)


def _iter_block_entries(block: bytes) -> Iterator[Tuple[bytes, bytes]]:
    """Iterate prefix-compressed `(key, value)` pairs inside a LevelDB table block."""
    if len(block) < 4:
        return
    (num_restarts,) = struct.unpack_from("<I", block, len(block) - 4)
    restarts_bytes = num_restarts * 4
    if 4 + restarts_bytes > len(block):
        return
    entries_end = len(block) - 4 - restarts_bytes

    pos = 0
    prev_key = b""
    while pos < entries_end:
        try:
            shared, pos = read_varint(block, pos)
            non_shared, pos = read_varint(block, pos)
            val_len, pos = read_varint(block, pos)
        except LevelDBFormatError:
            break
        if pos + non_shared + val_len > entries_end:
            break
        key = prev_key[:shared] + block[pos : pos + non_shared]
        pos += non_shared
        val = block[pos : pos + val_len]
        pos += val_len
        prev_key = key
        yield key, val


def _read_table_block(file_bytes: bytes, offset: int, size: int) -> Optional[bytes]:
    end = offset + size + LEVELDB_BLOCK_TRAILER_SIZE
    if offset < 0 or size < 0 or end > len(file_bytes):
        return None
    raw = file_bytes[offset : offset + size]
    compression_type = file_bytes[offset + size]
    if compression_type == 0:
        return raw
    if compression_type == 1:
        return snappy_decompress(raw)
    return None


def _iter_sstable_records(file_bytes: bytes) -> Iterator[Tuple[bytes, int, int, bytes]]:
    """Yield `(user_key, seq, value_type, value)` from a `.ldb` / `.sst` file."""
    if len(file_bytes) < LEVELDB_FOOTER_SIZE:
        return
    footer = file_bytes[-LEVELDB_FOOTER_SIZE:]
    (magic,) = struct.unpack_from("<Q", footer, 40)
    if magic != LEVELDB_TABLE_MAGIC:
        return

    try:
        # Footer starts with metaindex_handle followed by index_handle
        _, pos = read_varint(footer, 0)
        _, pos = read_varint(footer, pos)
        index_offset, pos = read_varint(footer, pos)
        index_size, _ = read_varint(footer, pos)
        index_block = _read_table_block(file_bytes, index_offset, index_size)
    except LevelDBFormatError:
        return

    if index_block is None:
        return

    for _, handle_bytes in _iter_block_entries(index_block):
        try:
            data_offset, hpos = read_varint(handle_bytes, 0)
            data_size, _ = read_varint(handle_bytes, hpos)
            data_block = _read_table_block(file_bytes, data_offset, data_size)
        except LevelDBFormatError:
            continue
        if data_block is None:
            continue
        for internal_key, val in _iter_block_entries(data_block):
            if len(internal_key) < 8:
                continue
            user_key = internal_key[:-8]
            (trailer,) = struct.unpack_from("<Q", internal_key, len(internal_key) - 8)
            seq = trailer >> 8
            vtype = trailer & 0xFF
            yield user_key, seq, vtype, val


def _iter_wal_records(file_bytes: bytes) -> Iterator[Tuple[bytes, int, int, bytes]]:
    """Yield `(user_key, seq, value_type, value)` from a LevelDB `.log` Write-Ahead Log."""
    batches: List[bytes] = []
    frag = bytearray()
    in_fragment = False

    for block_start in range(0, len(file_bytes), LEVELDB_LOG_BLOCK_SIZE):
        chunk = file_bytes[block_start : block_start + LEVELDB_LOG_BLOCK_SIZE]
        pos = 0
        while pos + LEVELDB_LOG_HEADER_SIZE <= len(chunk):
            _, length, rec_type = struct.unpack_from("<IHB", chunk, pos)
            pos += LEVELDB_LOG_HEADER_SIZE
            if rec_type == 0 or pos + length > len(chunk):
                break
            payload = chunk[pos : pos + length]
            pos += length

            if rec_type == LOG_FULL:
                frag.clear()
                in_fragment = False
                batches.append(payload)
            elif rec_type == LOG_FIRST:
                frag = bytearray(payload)
                in_fragment = True
            elif rec_type == LOG_MIDDLE and in_fragment:
                frag.extend(payload)
            elif rec_type == LOG_LAST and in_fragment:
                frag.extend(payload)
                batches.append(bytes(frag))
                frag.clear()
                in_fragment = False

    for batch in batches:
        if len(batch) < 12:
            continue
        base_seq, count = struct.unpack_from("<QI", batch, 0)
        pos = 12
        for idx in range(count):
            if pos >= len(batch):
                break
            vtype = batch[pos]
            pos += 1
            try:
                klen, pos = read_varint(batch, pos)
                if pos + klen > len(batch):
                    break
                user_key = batch[pos : pos + klen]
                pos += klen
                if vtype == K_TYPE_VALUE:
                    vlen, pos = read_varint(batch, pos)
                    if pos + vlen > len(batch):
                        break
                    val = batch[pos : pos + vlen]
                    pos += vlen
                    yield user_key, base_seq + idx, K_TYPE_VALUE, val
                elif vtype == K_TYPE_DELETION:
                    yield user_key, base_seq + idx, K_TYPE_DELETION, b""
                else:
                    break
            except LevelDBFormatError:
                break


def decode_chromium_dom_string(blob: bytes) -> Optional[str]:
    """Decode Chromium LocalStorage string prefixed with `0x00` (UTF-16LE) or `0x01` (Latin-1)."""
    if not blob:
        return ""
    prefix = blob[0]
    body = blob[1:]
    if prefix == 0x00:
        return body.decode("utf-16-le", errors="replace")
    if prefix == 0x01:
        return body.decode("latin-1")
    return None


def _parse_localstorage_user_key(user_key: bytes) -> Optional[Tuple[str, str]]:
    """Parse `_<origin>\\x00<encoded_js_key>` into `(canonical_origin, js_key)`."""
    if not user_key.startswith(b"_") or b"\x00" not in user_key:
        return None
    raw_origin, raw_js_key = user_key[1:].split(b"\x00", 1)
    # Skip third-party iframe partitioned StorageKeys containing '^'
    if b"^" in raw_origin:
        return None
    try:
        origin_text = raw_origin.decode("ascii").strip().rstrip("/")
    except UnicodeDecodeError:
        return None

    parsed = urlparse(origin_text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None

    js_key = decode_chromium_dom_string(raw_js_key)
    if js_key is None:
        return None
    return f"{parsed.scheme}://{parsed.netloc.lower()}", js_key


def _origin_matches_domains(
    origin: str,
    normalized_domains: Optional[Sequence[str]],
    *,
    all_domains: bool = False,
) -> bool:
    if all_domains or normalized_domains is None:
        return True
    if not normalized_domains:
        return False
    host = (urlparse(origin).hostname or "").lstrip(".").lower()
    for dom in normalized_domains:
        if host == dom or host.endswith("." + dom):
            return True
    return False


def _copy_regular_file_0600(src: Path, dst: Path) -> bool:
    """Copy a regular non-symlink file using O_NOFOLLOW into a 0600 destination."""
    read_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        src_fd = os.open(str(src), read_flags)
    except OSError:
        return False
    src_owned = True
    try:
        src_st = os.fstat(src_fd)
        if not stat.S_ISREG(src_st.st_mode):
            return False
        write_flags = os.O_CREAT | os.O_TRUNC | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        dst_fd = os.open(str(dst), write_flags, 0o600)
        dst_owned = True
        try:
            os.fchmod(dst_fd, 0o600)
            src_owned = False
            dst_owned = False
            with os.fdopen(src_fd, "rb") as rfh, os.fdopen(dst_fd, "wb") as wfh:
                shutil.copyfileobj(rfh, wfh)
            return True
        except OSError:
            if dst_owned:
                try:
                    os.close(dst_fd)
                except OSError:
                    pass
            try:
                dst.unlink()
            except OSError:
                pass
            return False
    finally:
        if src_owned:
            try:
                os.close(src_fd)
            except OSError:
                pass


def extract_local_storage(
    leveldb_dir: Path,
    normalized_domains: Optional[Sequence[str]],
    *,
    all_domains: bool = False,
) -> List[OriginEntry]:
    """Snapshot `<Profile>/Local Storage/leveldb` into an isolated 0700 temp dir and extract Playwright origins."""
    if leveldb_dir.is_symlink():
        raise LevelDBFormatError(f"Refusing to read symlinked LevelDB directory: {leveldb_dir}")
    if not leveldb_dir.is_dir():
        return []
    if not all_domains and normalized_domains is not None and len(normalized_domains) == 0:
        return []

    # Map user_key -> (highest_seq, vtype, raw_value)
    mvcc_latest: Dict[bytes, Tuple[int, int, bytes]] = {}

    with tempfile.TemporaryDirectory(prefix="sessfork_ls_") as tmp_str:
        tmp_dir = Path(tmp_str)
        os.chmod(tmp_dir, 0o700)

        copied_files: List[Path] = []
        for entry in sorted(leveldb_dir.iterdir()):
            if entry.is_symlink() or not entry.is_file() or entry.suffix.lower() not in (".ldb", ".sst", ".log"):
                continue
            dest = tmp_dir / entry.name
            if _copy_regular_file_0600(entry, dest):
                copied_files.append(dest)

        for fpath in copied_files:
            try:
                raw_bytes = fpath.read_bytes()
            except OSError:
                continue

            iterator = (
                _iter_wal_records(raw_bytes)
                if fpath.suffix.lower() == ".log"
                else _iter_sstable_records(raw_bytes)
            )
            for user_key, seq, vtype, val in iterator:
                if not user_key.startswith(b"_"):
                    continue
                prev = mvcc_latest.get(user_key)
                if prev is None or seq >= prev[0]:
                    mvcc_latest[user_key] = (seq, vtype, val)

    grouped: Dict[str, Dict[str, str]] = {}
    for user_key, (_, vtype, raw_val) in mvcc_latest.items():
        if vtype != K_TYPE_VALUE:
            continue
        parsed_key = _parse_localstorage_user_key(user_key)
        if parsed_key is None:
            continue
        origin, js_key = parsed_key
        if not _origin_matches_domains(origin, normalized_domains, all_domains=all_domains):
            continue
        js_val = decode_chromium_dom_string(raw_val)
        if js_val is None:
            continue
        grouped.setdefault(origin, {})[js_key] = js_val

    origins_out: List[OriginEntry] = []
    for origin in sorted(grouped.keys()):
        items = [
            {"name": k, "value": v}
            for k, v in sorted(grouped[origin].items(), key=lambda kv: kv[0])
        ]
        if items:
            origins_out.append({"origin": origin, "localStorage": items})

    return origins_out
