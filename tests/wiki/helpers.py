"""Fixture builders for the wiki generator tests."""

from __future__ import annotations

import struct
import zlib
from collections.abc import Sequence
from pathlib import Path

WIKI_TOML = """\
[home]
source = "README.md"

[[group]]
name = "User Guide"

[[group.page]]
slug = "first"
source = "first.md"

[[group.page]]
slug = "second"
source = "second.md"
"""


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def png_chunk(kind: bytes, data: bytes) -> bytes:
    body = kind + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))


def _png(
    width: int,
    height: int,
    rgb: tuple[int, int, int],
    extra: Sequence[tuple[bytes, bytes]] = (),
) -> bytes:
    """A solid-color PNG. `extra` (kind, data) chunks go between IHDR and IDAT."""
    row = b"\x00" + bytes(rgb) * width
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + b"".join(png_chunk(kind, data) for kind, data in extra)
        + png_chunk(b"IDAT", zlib.compress(row * height))
        + png_chunk(b"IEND", b"")
    )


def make_repo(root: Path) -> Path:
    """A fake repo root with a small, valid wiki."""
    write(root / "README.md", "# Graft\n\nThe project readme.\n")
    write(root / "LICENSE", "License text.\n")
    write(root / "docs/upgrading.md", "# Upgrading\n\nHow to upgrade.\n")
    wiki = root / "docs/wiki"
    write(
        wiki / "README.md",
        "# Graft Docs\n\nWelcome.\n\n- [First](first.md)\n- [Second](second.md)\n",
    )
    write(wiki / "wiki.toml", WIKI_TOML)
    write(wiki / "first.md", "# First Page\n\nThe first page.\n")
    write(wiki / "second.md", "# Second Page\n\nThe second page.\n")
    (wiki / "img").mkdir(parents=True, exist_ok=True)
    (wiki / "img/shot-light.png").write_bytes(_png(4, 2, (255, 255, 255)))
    (wiki / "img/shot-dark.png").write_bytes(_png(4, 2, (0, 0, 0)))
    return root


INDEX_HTML = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Graft</title>
<link rel="stylesheet" href="style.css">
</head>
<body>
<div class="wrap">
  <header class="bar">
    <a class="brand" href="./"><img src="icon.png" alt="" width="28" height="28">Graft</a>
    <nav aria-label="Site">
      <a href="#build">How it works</a>
      <a href="#install">Install</a>
      <a href="wiki/">Docs</a>
      <a href="https://github.com/GreenglassT/Graft">GitHub</a>
      <a href="https://ko-fi.com/greenglasst">Ko-fi</a>
    </nav>
  </header>
  <main>
    <h1>Graft</h1>
    <section id="build"><p>How it works.</p></section>
    <section id="install"><p>Install.</p></section>
  </main>
  <footer>
    <p>Made by <a href="https://github.com/GreenglassT">GreenglassT</a>. Also free: <a href="https://m4bapp.com">M4B Converter</a> and <a href="https://github.com/GreenglassT/SpoolBridge">SpoolBridge</a>.</p>
    <p>Graft is not affiliated with Apple Inc. Apple, iPhone, iPad, App Store and macOS are trademarks of Apple Inc.</p>
  </footer>
</div>
</body>
</html>
"""

SITEMAP_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://graftipa.com/</loc>
    <lastmod>2026-11-23</lastmod>
  </url>
</urlset>
"""


def make_site(root: Path) -> Path:
    """A fake site checkout: CNAME, style.css, index.html, sitemap.xml and the 1200×630
    link-preview card, og.png."""
    write(root / "CNAME", "graftipa.com\n")
    write(root / "style.css", "body { margin: 0; }\n")
    write(root / "index.html", INDEX_HTML)
    write(root / "sitemap.xml", SITEMAP_XML)
    (root / "og.png").write_bytes(_png(1200, 630, (16, 24, 40)))
    return root
