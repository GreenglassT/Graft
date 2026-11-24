#!/usr/bin/env python3
"""The Graft app icon: a splice, drawn as SVG.

Two pieces of stem joined on the diagonal, a white rootstock and a lavender
scion, held by green grafting tape, with a leaf and a bud at the tip. Glossy
light on the same slate and indigo as the earlier wrench icon.

The tile follows the macOS icon grid: an 824 px rounded square centered on a
1024 px canvas, with a transparent margin and a soft shadow.

`icon_svg()` is the full artwork. `icon_svg(small=True)` is a simpler drawing
for 16 and 32 px, with a thicker stem, bigger tape and no fine highlights.
"""
import math

import gen

S = gen.S
TILE = (100, 100, 824, 824)  # x, y, w, h of the rounded square
TILE_R = 185                 # its corner radius

# Leaf outline, pointing right from the origin (also used by the wiki mockups).
LEAF = ((0, 0), (50, -34), (120, -30), (170, 0), (120, 30), (50, 34), (0, 0))


def _cubic(p0, p1, p2, p3, steps=12):
    for i in range(steps + 1):
        t = i / steps
        u = 1 - t
        yield (u**3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t**3 * p3[0],
               u**3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t**3 * p3[1])


def _rotate(p, deg, about=(S / 2, S / 2)):
    a = math.radians(deg)
    x, y = p[0] - about[0], p[1] - about[1]
    return (about[0] + x * math.cos(a) - y * math.sin(a),
            about[1] + x * math.sin(a) + y * math.cos(a))


def _geometry(small):
    """The stem drawn vertical, centered on x=512, before it is turned 45 degrees."""
    g = dict(cx=512, hw=80 if small else 62, top=200, bot=852, y0=495, k=50 / 124,
             band_half=48 if small else 38, leaf_scale=1.8 if small else 1.5)
    g["leaf_base"] = (g["cx"] - g["hw"] + 2, g["top"] + (110 if small else 100))
    return g


def _leaf_points(g):
    """Sampled outline of the leaf, in the stem's own (unrotated) frame."""
    (bx, by), s, a = g["leaf_base"], g["leaf_scale"], math.radians(-150)
    pts = list(_cubic(*LEAF[0:4])) + list(_cubic(*LEAF[3:7]))
    return [(bx + s * (x * math.cos(a) - y * math.sin(a)),
             by + s * (x * math.sin(a) + y * math.cos(a))) for x, y in pts]


def _placement(g):
    """Scale and offset that center the turned symbol on the tile."""
    cx, hw, top, bot = g["cx"], g["hw"], g["top"], g["bot"]
    pts = [(cx - hw, top), (cx + hw, top), (cx - hw, bot), (cx + hw, bot),
           (cx, top - hw), (cx, bot + hw), (cx, top - 62), *_leaf_points(g)]
    turned = [_rotate(p, 45) for p in pts]
    xs, ys = [p[0] for p in turned], [p[1] for p in turned]
    w, h = max(xs) - min(xs), max(ys) - min(ys)
    scale = 637 / max(w, h)
    return scale, ((max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2)


def _paths(g):
    cx, hw, top, bot, y0, k = g["cx"], g["hw"], g["top"], g["bot"], g["y0"], g["k"]
    lx, rx = cx - hw, cx + hw
    scion = (f"M{lx} {top + hw} Q{lx} {top} {cx} {top} Q{rx} {top} {rx} {top + hw} "
             f"L{rx} {y0 - hw * k - 6:.1f} L{lx} {y0 + hw * k - 6:.1f} Z")
    stock = (f"M{lx} {y0 + hw * k + 6:.1f} L{rx} {y0 - hw * k + 6:.1f} L{rx} {bot - hw} "
             f"Q{rx} {bot} {cx} {bot} Q{lx} {bot} {lx} {bot - hw} Z")
    e, bh = hw + 10, g["band_half"]
    band = (f"M{cx - e} {y0 + e * k + bh:.1f} L{cx + e} {y0 - e * k + bh:.1f} "
            f"L{cx + e} {y0 - e * k - bh:.1f} L{cx - e} {y0 + e * k - bh:.1f} Z")
    bud = (f"M{cx} {top + 14} C{cx - 16} {top - 10} {cx - 14} {top - 40} {cx} {top - 62} "
           f"C{cx + 14} {top - 40} {cx + 16} {top - 10} {cx} {top + 14} Z")

    def seam_line(offset, inset=4):
        a, b = cx - hw + inset, cx + hw - inset
        return (f"M{a} {y0 - (a - cx) * k + offset:.1f} "
                f"L{b} {y0 - (b - cx) * k + offset:.1f}")

    return scion, stock, band, bud, seam_line


def _defs():
    return f'''<defs>
  <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{gen.BG_TOP}"/><stop offset="1" stop-color="{gen.BG_BOT}"/></linearGradient>
  <radialGradient id="glow" cx="0.5" cy="0.3" r="0.7"><stop offset="0" stop-color="{gen.ACCENT}" stop-opacity="0.32"/><stop offset="1" stop-color="{gen.ACCENT}" stop-opacity="0"/></radialGradient>
  <linearGradient id="sheen" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ffffff" stop-opacity="0.16"/><stop offset="0.45" stop-color="#ffffff" stop-opacity="0"/></linearGradient>
  <linearGradient id="stock" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#8f98cf"/><stop offset="0.28" stop-color="#ffffff"/><stop offset="0.62" stop-color="#dfe4ff"/><stop offset="1" stop-color="#7d86c2"/></linearGradient>
  <linearGradient id="scion" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#4338ca"/><stop offset="0.3" stop-color="{gen.IND_PALE}"/><stop offset="0.62" stop-color="{gen.ACCENT}"/><stop offset="1" stop-color="#3730a3"/></linearGradient>
  <linearGradient id="band" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#166534"/><stop offset="0.3" stop-color="#bbf7d0"/><stop offset="0.6" stop-color="#4ade80"/><stop offset="1" stop-color="#14532d"/></linearGradient>
  <linearGradient id="leaf" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#22c55e"/><stop offset="1" stop-color="#bbf7d0"/></linearGradient>
  <linearGradient id="spec" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ffffff" stop-opacity="0.75"/><stop offset="1" stop-color="#ffffff" stop-opacity="0.15"/></linearGradient>
  <filter id="tile-shadow" filterUnits="userSpaceOnUse" x="0" y="0" width="{S}" height="{S}"><feDropShadow dx="0" dy="14" stdDeviation="16" flood-color="#050d2b" flood-opacity="0.38"/></filter>
  <filter id="lift" filterUnits="userSpaceOnUse" x="0" y="0" width="{S}" height="{S}"><feDropShadow dx="0" dy="16" stdDeviation="18" flood-color="#040b26" flood-opacity="0.45"/></filter>
  <clipPath id="tile"><rect x="{TILE[0]}" y="{TILE[1]}" width="{TILE[2]}" height="{TILE[3]}" rx="{TILE_R}"/></clipPath>
</defs>'''


def symbol(small=False):
    """The turned, centered splice, without the tile."""
    g = _geometry(small)
    scion, stock, band, bud, seam_line = _paths(g)
    scale, (mx, my) = _placement(g)
    lx, ly = g["leaf_base"]
    ls = g["leaf_scale"]
    leaf_d = ("M0 0 C 50 -34 120 -30 170 0 C 120 30 50 34 0 0 Z")
    rib_len = 170 * ls * 0.8
    a = math.radians(-150)
    rib = f"M{lx} {ly} L{lx + rib_len * math.cos(a):.1f} {ly + rib_len * math.sin(a):.1f}"
    parts = [f'<g transform="translate({S / 2} {S / 2}) scale({scale:.4f}) '
             f'translate({-mx:.1f} {-my:.1f})"><g transform="rotate(45 {S / 2} {S / 2})">',
             f'<clipPath id="rod{int(small)}"><path d="{scion}"/><path d="{stock}"/></clipPath>',
             f'<g filter="url(#lift)"><path d="{stock}" fill="url(#stock)"/>'
             f'<path d="{scion}" fill="url(#scion)"/></g>']
    if not small:
        cx, top, bot = g["cx"], g["top"], g["bot"]
        parts += [f'<g clip-path="url(#rod0)"><rect x="{cx - 42}" y="{top - 10}" width="22" '
                  f'height="{bot - top + 20}" rx="11" fill="url(#spec)"/></g>',
                  f'<path d="{stock}" fill="none" stroke="#3b4382" stroke-opacity="0.55" stroke-width="4"/>',
                  f'<path d="{scion}" fill="none" stroke="#312e81" stroke-opacity="0.6" stroke-width="4"/>']
    parts.append(f'<path d="{band}" fill="url(#band)" stroke="url(#band)" stroke-width="10" '
                 f'stroke-linejoin="round"/>')
    if small:
        parts.append(f'<path d="{seam_line(0, 0)}" stroke="#14532d" stroke-opacity="0.7" '
                     f'stroke-width="12" stroke-linecap="round"/>')
    else:
        parts += [f'<g stroke="#14532d" stroke-opacity="0.7" stroke-width="7" stroke-linecap="round">'
                  f'<path d="{seam_line(-8)}"/><path d="{seam_line(18)}"/></g>',
                  f'<path d="{seam_line(-34, 8)}" stroke="#ffffff" stroke-opacity="0.55" '
                  f'stroke-width="5" stroke-linecap="round"/>']
    parts.append(f'<path d="{leaf_d}" transform="translate({lx} {ly}) rotate(-150) scale({ls})" '
                 f'fill="url(#leaf)"/>')
    if not small:
        parts += [f'<path d="{rib}" stroke="#15803d" stroke-width="8" stroke-linecap="round"/>',
                  f'<path d="{bud}" fill="#86efac"/>']
    parts.append("</g></g>")
    return "".join(parts)


def icon_svg(small=False):
    x, y, w, h = TILE
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{S}" height="{S}" '
            f'viewBox="0 0 {S} {S}">{_defs()}'
            f'<g filter="url(#tile-shadow)"><rect x="{x}" y="{y}" width="{w}" height="{h}" '
            f'rx="{TILE_R}" fill="url(#bg)"/></g>'
            f'<g clip-path="url(#tile)">'
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="url(#glow)"/>'
            f'{symbol(small)}'
            f'<rect x="{x}" y="{y}" width="{w}" height="{h / 2}" rx="{TILE_R}" fill="url(#sheen)"/>'
            f'<rect x="{x + 3}" y="{y + 3}" width="{w - 6}" height="{h - 6}" rx="{TILE_R - 3}" '
            f'fill="none" stroke="{gen.LINE}" stroke-opacity="0.7" stroke-width="3"/>'
            f'</g></svg>')


if __name__ == "__main__":
    import os
    out = os.path.dirname(os.path.abspath(__file__))
    for name, small in (("splice.svg", False), ("splice-small.svg", True)):
        with open(os.path.join(out, name), "w") as f:
            f.write(icon_svg(small))
        print("wrote", name)
