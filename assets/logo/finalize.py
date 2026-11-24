#!/usr/bin/env python3
"""Build every Graft logo asset from the splice artwork in splice.py.

Writes the vector icons, the wordmark and the banner as SVG, then renders them
with rsvg-convert:

- assets/logo/png/icon-{128,256,512,1024}.png, the wordmark and the banner;
- the app's AppIcon set, at every size its Contents.json lists. The 16 and
  32 px images use the simpler small drawing; 64 px and up use the full one.

Run from anywhere: `python3 assets/logo/finalize.py`. Needs rsvg-convert
(Homebrew: librsvg).
"""
import json
import os
import shutil
import subprocess

import gen
import splice

S = gen.S
OUT = os.path.dirname(os.path.abspath(__file__))
PNG = os.path.join(OUT, "png")
PROJ = os.path.abspath(os.path.join(OUT, "..", "..", "Graft", "Graft"))
XCASSETS = os.path.join(PROJ, "Assets.xcassets")
APPICON = os.path.join(XCASSETS, "AppIcon.appiconset")

# The visible rounded square fills this share of the icon canvas.
TILE_SHARE = splice.TILE[2] / S


def _icon_image(x, y, tile, href="png/icon-512.png"):
    """An <image> of the icon whose rounded square lands on (x, y, tile, tile)."""
    size = tile / TILE_SHARE
    inset = splice.TILE[0] / S * size
    return (f'<image href="{href}" x="{x - inset:.1f}" y="{y - inset:.1f}" '
            f'width="{size:.1f}" height="{size:.1f}"/>')


def wordmark_svg():
    H, ic, pad = 300, 232, 34
    tx = pad + ic + 34
    return (f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="1180" height="{H}" viewBox="0 0 1180 {H}">'
            f'{_icon_image(pad, (H - ic) // 2, ic)}'
            f'<text x="{tx}" y="{H/2}" dominant-baseline="central" '
            f'font-family="SF Pro Display, Helvetica Neue, Arial, sans-serif" '
            f'font-size="150" font-weight="800" letter-spacing="-3">'
            f'<tspan fill="{gen.ACCENT}">graft</tspan></text></svg>')


def banner_svg():
    W, H, ic, pad = 1280, 340, 244, 48
    tx = pad + ic + 40
    return (f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{W}" height="{H}" viewBox="0 0 {W} {H}">{gen.defs()}'
            f'<rect width="{W}" height="{H}" rx="56" fill="url(#bg)"/>'
            f'<rect width="{W}" height="{H}" rx="56" fill="url(#glow)"/>'
            f'<rect x="2" y="2" width="{W-4}" height="{H-4}" rx="54" '
            f'fill="none" stroke="{gen.LINE}" stroke-width="2" stroke-opacity="0.7"/>'
            f'{_icon_image(pad, (H - ic) // 2, ic)}'
            f'<text x="{tx}" y="148" '
            f'font-family="SF Pro Display, Helvetica Neue, Arial, sans-serif" '
            f'font-size="128" font-weight="800" letter-spacing="-3">'
            f'<tspan fill="{gen.TEXT}">graft</tspan></text>'
            f'<text x="{tx+4}" y="232" fill="{gen.IND_PALE}" '
            f'font-family="SF Pro Text, Helvetica Neue, Arial, sans-serif" '
            f'font-size="46" font-weight="500" letter-spacing="0.5">'
            f'iOS tweak injection &amp; sideload</text></svg>')


def appicon_contents():
    sizes = [(16, "1x", 16), (16, "2x", 32), (32, "1x", 32), (32, "2x", 64),
             (128, "1x", 128), (128, "2x", 256), (256, "1x", 256), (256, "2x", 512),
             (512, "1x", 512), (512, "2x", 1024)]
    images = [{"size": f"{s}x{s}", "idiom": "mac",
               "filename": f"icon-{px}.png", "scale": scale}
              for s, scale, px in sizes]
    return {"images": images, "info": {"version": 1, "author": "xcode"}}


def _render(svg_path, png_path, width, height=None):
    subprocess.run(["rsvg-convert", "-w", str(width), "-h", str(height or width),
                    svg_path, "-o", png_path], check=True)


def main():
    if not shutil.which("rsvg-convert"):
        raise SystemExit("finalize.py needs rsvg-convert (brew install librsvg)")

    full = os.path.join(OUT, "icon-macos.svg")
    small = os.path.join(OUT, "icon-macos-small.svg")
    files = {full: splice.icon_svg(), small: splice.icon_svg(small=True),
             os.path.join(OUT, "wordmark.svg"): wordmark_svg(),
             os.path.join(OUT, "banner.svg"): banner_svg()}
    for path, text in files.items():
        with open(path, "w") as f:
            f.write(text)

    os.makedirs(PNG, exist_ok=True)
    for px in (128, 256, 512, 1024):
        _render(full, os.path.join(PNG, f"icon-{px}.png"), px)
    _render(os.path.join(OUT, "wordmark.svg"), os.path.join(PNG, "wordmark.png"), 1180, 300)
    _render(os.path.join(OUT, "banner.svg"), os.path.join(PNG, "banner.png"), 1280, 340)

    os.makedirs(APPICON, exist_ok=True)
    contents = appicon_contents()
    with open(os.path.join(APPICON, "Contents.json"), "w") as f:
        json.dump(contents, f, indent=2)
    for px in sorted({int(e["filename"][5:-4]) for e in contents["images"]}):
        _render(small if px <= 32 else full, os.path.join(APPICON, f"icon-{px}.png"), px)
    cat = os.path.join(XCASSETS, "Contents.json")
    if not os.path.exists(cat):
        with open(cat, "w") as f:
            json.dump({"info": {"version": 1, "author": "xcode"}}, f, indent=2)
    print("wrote the logo SVGs and PNGs, and the AppIcon set in", APPICON)


if __name__ == "__main__":
    main()
