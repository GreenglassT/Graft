#!/usr/bin/env python3
"""Build final Graft logo assets from the approved raster icon."""
import os, json
import gen

S = gen.S
OUT = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.abspath(os.path.join(OUT, "..", "..", "Graft", "Graft"))
XCASSETS = os.path.join(PROJ, "Assets.xcassets")
APPICON = os.path.join(XCASSETS, "AppIcon.appiconset")


def macos_svg():
    """Portable SVG wrapper around the approved 1024 px source artwork."""
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{S}" height="{S}" '
            f'viewBox="0 0 {S} {S}">'
            f'<clipPath id="icon-clip"><rect width="{S}" height="{S}" rx="220"/></clipPath>'
            f'<image href="png/icon-1024.png" width="{S}" height="{S}" '
            f'clip-path="url(#icon-clip)"/>'
            f'</svg>')


def wordmark_svg():
    H, ic, pad = 300, 232, 34
    tx = pad + ic + 34
    href = "png/icon-256.png"
    return (f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'xmlns:xlink="http://www.w3.org/1999/xlink" '
            f'width="1180" height="{H}" viewBox="0 0 1180 {H}">'
            f'<clipPath id="icon-clip"><rect x="{pad}" y="{(H-ic)//2}" '
            f'width="{ic}" height="{ic}" rx="52"/></clipPath>'
            f'<image href="{href}" x="{pad}" y="{(H-ic)//2}" width="{ic}" height="{ic}" '
            f'clip-path="url(#icon-clip)"/>'
            f'<text x="{tx}" y="{H/2}" dominant-baseline="central" '
            f'font-family="SF Pro Display, Helvetica Neue, Arial, sans-serif" '
            f'font-size="150" font-weight="800" letter-spacing="-3">'
            f'<tspan fill="{gen.ACCENT}">graft</tspan></text></svg>')


def banner_svg():
    W, H, ic, pad = 1280, 340, 244, 48
    tx = pad + ic + 40
    href = "png/icon-256.png"
    return (f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'xmlns:xlink="http://www.w3.org/1999/xlink" '
            f'width="{W}" height="{H}" viewBox="0 0 {W} {H}">{gen.defs()}'
            f'<rect width="{W}" height="{H}" rx="56" fill="url(#bg)"/>'
            f'<rect width="{W}" height="{H}" rx="56" fill="url(#glow)"/>'
            f'<rect x="2" y="2" width="{W-4}" height="{H-4}" rx="54" '
            f'fill="none" stroke="{gen.LINE}" stroke-width="2" stroke-opacity="0.7"/>'
            f'<clipPath id="icon-clip"><rect x="{pad}" y="{(H-ic)//2}" '
            f'width="{ic}" height="{ic}" rx="55"/></clipPath>'
            f'<image href="{href}" x="{pad}" y="{(H-ic)//2}" width="{ic}" height="{ic}" '
            f'clip-path="url(#icon-clip)"/>'
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


def main():
    with open(os.path.join(OUT, "icon-macos.svg"), "w") as f:
        f.write(macos_svg())
    with open(os.path.join(OUT, "wordmark.svg"), "w") as f:
        f.write(wordmark_svg())
    with open(os.path.join(OUT, "banner.svg"), "w") as f:
        f.write(banner_svg())

    os.makedirs(APPICON, exist_ok=True)
    with open(os.path.join(APPICON, "Contents.json"), "w") as f:
        json.dump(appicon_contents(), f, indent=2)
    cat = os.path.join(XCASSETS, "Contents.json")
    if not os.path.exists(cat):
        with open(cat, "w") as f:
            json.dump({"info": {"version": 1, "author": "xcode"}}, f, indent=2)
    print("wrote SVGs + appiconset JSON. APPICON =", APPICON)


if __name__ == "__main__":
    main()
