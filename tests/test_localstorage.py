"""Unit tests for LevelDB LocalStorage parsing and Playwright origins state merging."""

from __future__ import annotations

import json
import os
import struct
import tempfile
import unittest
from pathlib import Path

from sessfork.cli import main
from sessfork.localstorage import (
    K_TYPE_DELETION,
    K_TYPE_VALUE,
    LEVELDB_TABLE_MAGIC,
    LOG_FIRST,
    LOG_FULL,
    LOG_LAST,
    extract_local_storage,
    snappy_decompress,
)
from sessfork.state import clear_state, load_storage_state, summarize_state, upsert_session_state


def _encode_varint(val: int) -> bytes:
    out = bytearray()
    while val >= 0x80:
        out.append((val & 0x7F) | 0x80)
        val >>= 7
    out.append(val)
    return bytes(out)


def _snappy_literal_plus_copy(prefix: bytes, repeat_offset: int, repeat_len: int) -> bytes:
    total_len = len(prefix) + repeat_len
    stream = bytearray(_encode_varint(total_len))
    lit_tag = ((len(prefix) - 1) << 2) | 0x00
    stream.append(lit_tag)
    stream.extend(prefix)
    copy_tag = (((repeat_len - 4) & 0x07) << 2) | 0x01 | (((repeat_offset >> 8) & 0x07) << 5)
    stream.append(copy_tag)
    stream.append(repeat_offset & 0xFF)
    return bytes(stream)


def _build_block(entries: list[tuple[bytes, bytes]]) -> bytes:
    buf = bytearray()
    prev = b""
    for k, v in entries:
        shared = 0
        max_sh = min(len(prev), len(k))
        while shared < max_sh and prev[shared] == k[shared]:
            shared += 1
        buf.extend(_encode_varint(shared))
        buf.extend(_encode_varint(len(k) - shared))
        buf.extend(_encode_varint(len(v)))
        buf.extend(k[shared:])
        buf.extend(v)
        prev = k
    buf.extend(struct.pack("<II", 0, 1))
    return bytes(buf)


def _make_internal_key(user_key: bytes, seq: int, vtype: int) -> bytes:
    return user_key + struct.pack("<Q", (seq << 8) | vtype)


def _build_sstable(records: list[tuple[bytes, int, int, bytes]], use_snappy: bool = False) -> bytes:
    data_entries = [(_make_internal_key(uk, seq, vt), val) for uk, seq, vt, val in records]
    raw_data_block = _build_block(data_entries)

    if use_snappy:
        # Compress raw_data_block as a single Snappy literal stream
        comp = bytearray(_encode_varint(len(raw_data_block)))
        n = len(raw_data_block) - 1
        if n < 60:
            comp.append((n << 2) | 0x00)
        else:
            comp.append((60 << 2) | 0x00)
            comp.append(n & 0xFF)
        comp.extend(raw_data_block)
        stored_data = bytes(comp)
        ctype = 1
    else:
        stored_data = raw_data_block
        ctype = 0

    file_buf = bytearray(stored_data)
    file_buf.extend(struct.pack("<BI", ctype, 0))

    meta_offset = len(file_buf)
    meta_block = _build_block([])
    file_buf.extend(meta_block)
    file_buf.extend(struct.pack("<BI", 0, 0))

    index_offset = len(file_buf)
    handle = _encode_varint(0) + _encode_varint(len(stored_data))
    last_key = data_entries[-1][0] if data_entries else b"z"
    index_block = _build_block([(last_key, handle)])
    file_buf.extend(index_block)
    file_buf.extend(struct.pack("<BI", 0, 0))

    footer_handles = (
        _encode_varint(meta_offset)
        + _encode_varint(len(meta_block))
        + _encode_varint(index_offset)
        + _encode_varint(len(index_block))
    )
    footer = footer_handles.ljust(40, b"\x00") + struct.pack("<Q", LEVELDB_TABLE_MAGIC)
    file_buf.extend(footer)
    return bytes(file_buf)


def _build_wal_batch(base_seq: int, ops: list[tuple[int, bytes, bytes]]) -> bytes:
    batch = bytearray(struct.pack("<QI", base_seq, len(ops)))
    for vtype, k, v in ops:
        batch.append(vtype)
        batch.extend(_encode_varint(len(k)))
        batch.extend(k)
        if vtype == K_TYPE_VALUE:
            batch.extend(_encode_varint(len(v)))
            batch.extend(v)
    return bytes(batch)


class LocalStorageTestSuite(unittest.TestCase):
    def test_snappy_backreference_and_rle(self) -> None:
        compressed = _snappy_literal_plus_copy(b"abcdef", repeat_offset=6, repeat_len=6)
        self.assertEqual(snappy_decompress(compressed), b"abcdefabcdef")

        rle_stream = _snappy_literal_plus_copy(b"x", repeat_offset=1, repeat_len=5)
        self.assertEqual(snappy_decompress(rle_stream), b"xxxxxx")

    def test_sstable_and_wal_mvcc_tombstone_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ldb_dir = Path(tmp) / "Default" / "Local Storage" / "leveldb"
            ldb_dir.mkdir(parents=True)

            k_theme = b"_https://app.linear.app\x00\x01ui_theme"
            k_stale = b"_https://app.linear.app\x00\x01temp_draft"
            k_utf16 = b"_https://app.linear.app\x00\x00" + "语言".encode("utf-16-le")
            k_other = b"_https://other.site\x00\x01mode"

            sst_bytes = _build_sstable(
                [
                    (k_utf16, 1, K_TYPE_VALUE, b"\x00" + "中文界面".encode("utf-16-le")),
                    (k_stale, 2, K_TYPE_VALUE, b"\x01old_draft"),
                    (k_theme, 3, K_TYPE_VALUE, b"\x01light"),
                    (k_other, 4, K_TYPE_VALUE, b"\x01compact"),
                ],
                use_snappy=True,
            )
            (ldb_dir / "000005.ldb").write_bytes(sst_bytes)

            # WAL overrides ui_theme -> dark (seq 10) and deletes temp_draft via tombstone (seq 11)
            wal_batch = _build_wal_batch(
                10,
                [
                    (K_TYPE_VALUE, k_theme, b"\x01dark"),
                    (K_TYPE_DELETION, k_stale, b""),
                ],
            )
            half = len(wal_batch) // 2
            rec1 = struct.pack("<IHB", 0, half, LOG_FIRST) + wal_batch[:half]
            rec2 = struct.pack("<IHB", 0, len(wal_batch) - half, LOG_LAST) + wal_batch[half:]
            (ldb_dir / "000006.log").write_bytes(rec1 + rec2)

            origins = extract_local_storage(ldb_dir, ["linear.app"])
            self.assertEqual(len(origins), 1)
            self.assertEqual(origins[0]["origin"], "https://app.linear.app")
            kv = {item["name"]: item["value"] for item in origins[0]["localStorage"]}
            self.assertEqual(kv, {"ui_theme": "dark", "语言": "中文界面"})
            self.assertNotIn("temp_draft", kv)

            # End-to-end CLI --no-cookies test + redacted summary + clear
            state_file = Path(tmp) / "state.json"
            rc = main(
                [
                    "linear.app",
                    "--no-cookies",
                    "--leveldb-path",
                    str(ldb_dir),
                    "--state-file",
                    str(state_file),
                    "--target",
                    "none",
                ]
            )
            self.assertEqual(rc, 0)

            loaded = load_storage_state(state_file)
            self.assertEqual(len(loaded["origins"]), 1)

            summary = summarize_state(state_file)
            self.assertEqual(summary["total_local_storage_keys"], 2)
            self.assertNotIn("中文界面", json.dumps(summary, ensure_ascii=False))

            _, _, rem_o, left_o = clear_state(["linear.app"], state_path=state_file)
            self.assertEqual(rem_o, 1)
            self.assertEqual(left_o, 0)

    def test_symlinked_leveldb_files_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            secret_sst = tmp_path / "outside.ldb"
            secret_sst.write_bytes(
                _build_sstable([(b"_https://evil.example\x00\x01token", 1, K_TYPE_VALUE, b"\x01leaked")])
            )
            ldb_dir = tmp_path / "leveldb"
            ldb_dir.mkdir()
            os.symlink(secret_sst, ldb_dir / "000001.ldb")

            origins = extract_local_storage(ldb_dir, ["evil.example"])
            self.assertEqual(origins, [])


if __name__ == "__main__":
    unittest.main()
