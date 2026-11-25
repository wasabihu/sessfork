#!/usr/bin/env python3
"""Build Microsoft Edge Add-ons (Partner Center) compliant package & vector PNG logo.

Why a separate Edge Store package?
Microsoft Partner Center strictly rejects `manifest.json` files that contain a `"key"`
field ("The manifest shouldn't contain the key field"), whereas local unpacked installs
keep `"key"` to preserve the deterministic ID `eipgnmlpcpmadpbignmifidnbjfmlphm`.

Outputs (under `dist/edge-store/`):
  - `sessfork-edge-store-<version>.zip`  (key stripped, clean MV3 files only)
  - `logo-300x300.png`                   (4x supersampled Edge Partner Center required logo)
"""

from __future__ import annotations

import json
import math
import pathlib
import struct
import zipfile
import zlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXT_DIR = ROOT / "extension"
OUT_DIR = ROOT / "dist" / "edge-store"


def _dist_seg(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    c1 = vx * wx + vy * wy
    if c1 <= 0.0:
        return math.hypot(px - ax, py - ay)
    c2 = vx * vx + vy * vy
    if c2 <= c1:
        return math.hypot(px - bx, py - by)
    t = c1 / c2
    return math.hypot(px - (ax + t * vx), py - (ay + t * vy))


def _sdf_round_rect(x: float, y: float, half: float, radius: float) -> float:
    dx = abs(x) - (half - radius)
    dy = abs(y) - (half - radius)
    return math.hypot(max(dx, 0.0), max(dy, 0.0)) + min(max(dx, dy), 0.0) - radius


def _sample_logo(u: float, v: float) -> tuple[float, float, float, float]:
    cx, cy = u - 0.5, v - 0.5
    if _sdf_round_rect(cx, cy, 0.47, 0.13) > 0.0:
        return (0.0, 0.0, 0.0, 0.0)
    g = max(0.0, min(1.0, (u + v) * 0.5))
    r = 16.0 * (1.0 - g) + 4.0 * g
    gr = 185.0 * (1.0 - g) + 120.0 * g
    b = 129.0 * (1.0 - g) + 87.0 * g

    stroke_r, node_r, hole_r = 0.046, 0.088, 0.040
    d_stem = _dist_seg(u, v, 0.50, 0.50, 0.50, 0.74) - stroke_r
    d_left = min(
        _dist_seg(u, v, 0.32, 0.28, 0.32, 0.42),
        _dist_seg(u, v, 0.32, 0.42, 0.50, 0.52),
    ) - stroke_r
    d_right = min(
        _dist_seg(u, v, 0.68, 0.28, 0.68, 0.42),
        _dist_seg(u, v, 0.68, 0.42, 0.50, 0.52),
    ) - stroke_r
    d_nodes = min(
        math.hypot(u - 0.32, v - 0.28) - node_r,
        math.hypot(u - 0.68, v - 0.28) - node_r,
        math.hypot(u - 0.50, v - 0.74) - node_r,
    )
    d_holes = min(
        math.hypot(u - 0.32, v - 0.28) - hole_r,
        math.hypot(u - 0.68, v - 0.28) - hole_r,
        math.hypot(u - 0.50, v - 0.74) - hole_r,
    )
    if min(d_stem, d_left, d_right, d_nodes) <= 0.0 and d_holes > 0.0:
        return (255.0, 255.0, 255.0, 255.0)
    return (r, gr, b, 255.0)


def render_logo_png(path: pathlib.Path, size: int = 300, ss: int = 3) -> None:
    raw = bytearray()
    n = float(ss * ss)
    for y in range(size):
        raw.append(0)
        for x in range(size):
            sr = sg = sb = sa = 0.0
            for sy in range(ss):
                v = (y + (sy + 0.5) / ss) / size
                for sx in range(ss):
                    u = (x + (sx + 0.5) / ss) / size
                    cr, cg, cb, ca = _sample_logo(u, v)
                    f = ca / 255.0
                    sr += cr * f
                    sg += cg * f
                    sb += cb * f
                    sa += ca
            alpha = sa / n
            if alpha > 0.5:
                inv = 255.0 / sa
                raw.extend((
                    int(max(0, min(255, round(sr * inv)))),
                    int(max(0, min(255, round(sg * inv)))),
                    int(max(0, min(255, round(sb * inv)))),
                    int(max(0, min(255, round(alpha)))),
                ))
            else:
                raw.extend((0, 0, 0, 0))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(png)


def build_edge_zip() -> tuple[pathlib.Path, str]:
    manifest = json.loads((EXT_DIR / "manifest.json").read_text(encoding="utf-8"))
    version = manifest["version"]
    # Strip "key": Partner Center rejects packages containing a manifest "key" field.
    manifest.pop("key", None)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = OUT_DIR / f"sessfork-edge-store-{version}.zip"
    include_files = [
        "popup.html",
        "popup.js",
        "icons/icon16.png",
        "icons/icon48.png",
        "icons/icon128.png",
    ]
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")
        for rel in include_files:
            src = EXT_DIR / rel
            if not src.is_file():
                raise FileNotFoundError(f"Missing required extension file: {src}")
            zf.write(src, rel)

    # Verify the generated archive has no "key" field and valid JSON.
    with zipfile.ZipFile(zip_path, "r") as zf:
        check = json.loads(zf.read("manifest.json").decode("utf-8"))
        assert "key" not in check, "manifest.json in Edge Store ZIP must NOT contain 'key'"
        assert check["manifest_version"] == 3
    return zip_path, version


def main() -> None:
    zip_path, version = build_edge_zip()
    logo_path = OUT_DIR / "logo-300x300.png"
    render_logo_png(logo_path, size=300, ss=3)
    print(f"OK: Edge Store ZIP  -> {zip_path} ({zip_path.stat().st_size} bytes, version={version}, key stripped)")
    print(f"OK: Store Logo      -> {logo_path} ({logo_path.stat().st_size} bytes, 300x300 PNG)")


if __name__ == "__main__":
    main()
