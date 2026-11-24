"""The app icon set is complete, sized right, and shaped like a Mac icon.

A Mac app icon is a rounded square that sits inside a transparent margin, so
the Dock and Finder show the shape rather than a solid square. These checks
read the PNGs the asset catalog compiles into Assets.car.
"""

from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
APPICON = REPO / "Graft" / "Graft" / "Assets.xcassets" / "AppIcon.appiconset"
LOGO_PNG = REPO / "assets" / "logo" / "png"


def _png(path: Path) -> tuple[int, int, int, bytes]:
    """Width, height, color type and the decompressed image data of a PNG."""
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG"
    pos, idat = 8, b""
    width = height = color = depth = 0
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        if kind == b"IHDR":
            width, height, depth, color = struct.unpack(">IIBB", body[:10])
        elif kind == b"IDAT":
            idat += body
        pos += 12 + length
    assert depth == 8, f"{path.name}: expected 8-bit channels"
    return width, height, color, zlib.decompress(idat)


def _rows(width: int, raw: bytes, bpp: int, count: int) -> list[bytes]:
    """Undo PNG row filters for the first `count` rows."""
    stride = width * bpp
    prev = bytearray(stride)
    out = []
    for r in range(count):
        start = r * (stride + 1)
        ftype, line = raw[start], bytearray(raw[start + 1 : start + 1 + stride])
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            if ftype == 1:
                line[i] = (line[i] + a) & 0xFF
            elif ftype == 2:
                line[i] = (line[i] + b) & 0xFF
            elif ftype == 3:
                line[i] = (line[i] + (a + b) // 2) & 0xFF
            elif ftype == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[i] = (line[i] + pred) & 0xFF
        out.append(bytes(line))
        prev = line
    return out


def _alpha(path: Path, x: int, y: int) -> int:
    width, _, color, raw = _png(path)
    assert color == 6, f"{path.name} has no alpha channel (color type {color})"
    row = _rows(width, raw, 4, y + 1)[y]
    return row[x * 4 + 3]


def _contents() -> list[dict]:
    return json.loads((APPICON / "Contents.json").read_text())["images"]


def test_every_listed_icon_exists_at_its_declared_size():
    images = _contents()
    assert images, "Contents.json lists no images"
    for entry in images:
        path = APPICON / entry["filename"]
        assert path.is_file(), f"missing {entry['filename']}"
        points = int(entry["size"].split("x")[0])
        scale = int(entry["scale"].rstrip("x"))
        width, height, _, _ = _png(path)
        assert (width, height) == (points * scale, points * scale), entry["filename"]


@pytest.mark.parametrize("name", sorted({e["filename"] for e in _contents()}))
def test_icon_has_transparent_corners_and_an_opaque_middle(name):
    path = APPICON / name
    width, height, _, _ = _png(path)
    assert _alpha(path, 0, 0) == 0, f"{name}: the corner should be transparent"
    assert _alpha(path, width // 2, height // 2) == 255, f"{name}: the middle should be opaque"


def test_logo_pngs_match_the_app_icon():
    """assets/logo/png carries the same artwork the README and wordmark use."""
    for size in (128, 256, 512, 1024):
        logo = LOGO_PNG / f"icon-{size}.png"
        assert logo.is_file(), f"missing assets/logo/png/icon-{size}.png"
        assert _alpha(logo, 0, 0) == 0, f"icon-{size}.png corner should be transparent"
