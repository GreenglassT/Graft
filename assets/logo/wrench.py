#!/usr/bin/env python3
"""Detailed realistic combination wrench, glossy indigo on slate. Graft logo."""
import math, os
import gen  # reuse defs(), background(), svg_wrap(), palette, S

S = gen.S
OUT = os.path.dirname(os.path.abspath(__file__))


def hexagon(cx, cy, r, rot=0, ccw=False):
    pts = []
    for i in range(6):
        a = math.radians(rot + 60 * i)
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    if ccw:
        pts = [pts[0]] + pts[1:][::-1]   # reverse winding (keep start)
    d = "M %.2f %.2f " % pts[0] + " ".join("L %.2f %.2f" % p for p in pts[1:]) + " Z"
    return d


def circle_cw(cx, cy, r):
    return (f"M {cx+r:.2f} {cy:.2f} "
            f"A {r:.2f} {r:.2f} 0 1 1 {cx-r:.2f} {cy:.2f} "
            f"A {r:.2f} {r:.2f} 0 1 1 {cx+r:.2f} {cy:.2f} Z")


def _rot(pts, deg, px, py):
    a = math.radians(deg)
    ca, sa = math.cos(a), math.sin(a)
    out = []
    for x, y in pts:
        dx, dy = x - px, y - py
        out.append((px + dx * ca - dy * sa, py + dx * sa + dy * ca))
    return out


def poly(pts):
    return "M %.2f %.2f " % pts[0] + " ".join("L %.2f %.2f" % p for p in pts[1:]) + " Z"


def wrench_path():
    # --- box (ring) end on the left ---
    C1x, R1, RH = -250.0, 118.0, 72.0
    box = circle_cw(C1x, 0, R1)
    hole = hexagon(C1x, 0, RH, rot=8, ccw=True)

    # --- tapered handle (waisted bar) ---
    hL, hR, wE, wC = -250.0, 205.0, 70.0, 44.0
    handle = (f"M {hL:.1f} {-wE:.1f} Q 0 {-wC:.1f} {hR:.1f} {-wE:.1f} "
              f"L {hR:.1f} {wE:.1f} Q 0 {wC:.1f} {hL:.1f} {wE:.1f} Z")

    # --- open end on the right (block with U jaw), tilted ~13 deg ---
    L, Rr, T, B = 175.0, 337.0, -125.0, 125.0
    jr = Rr - 118.0          # jaw root x
    g = 48.0                 # half jaw gap
    head_pts = [(L, T), (Rr, T), (Rr, -g), (jr, -g),
                (jr, g), (Rr, g), (Rr, B), (L, B)]
    head_pts = _rot(head_pts, 13, L, 0)
    head = poly(head_pts)

    return " ".join([box, hole, handle, head])


def inner():
    d = wrench_path()
    s = 1.06
    theta = -28
    cx0 = (-368 + 337) / 2      # centroid x of local bbox
    tf = (f'translate({S/2} {S/2}) rotate({theta}) scale({s}) '
          f'translate({-cx0:.1f} 0)')

    extra_defs = f'''<defs>
      <linearGradient id="metal" x1="0" y1="0" x2="0.4" y2="1">
        <stop offset="0"   stop-color="{gen.IND_PALE}"/>
        <stop offset="0.45" stop-color="{gen.ACCENT}"/>
        <stop offset="1"   stop-color="{gen.IND_DEEP}"/>
      </linearGradient>
      <linearGradient id="sheen" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0"    stop-color="#FFFFFF" stop-opacity="0.42"/>
        <stop offset="0.42" stop-color="#FFFFFF" stop-opacity="0.05"/>
        <stop offset="1"    stop-color="#FFFFFF" stop-opacity="0"/>
      </linearGradient>
      <clipPath id="wr" clipPathUnits="userSpaceOnUse">
        <path d="{d}" transform="{tf}"/>
      </clipPath>
    </defs>'''

    # base: drop shadow + corner-rounding stroke + metal fill
    base = (f'<g filter="url(#ds)">'
            f'<path d="{d}" transform="{tf}" fill="url(#metal)" '
            f'stroke="url(#metal)" stroke-width="20" '
            f'stroke-linejoin="round" stroke-linecap="round"/>'
            f'</g>')

    # glossy top sheen, clipped to the wrench silhouette
    gloss = (f'<g clip-path="url(#wr)">'
             f'<rect x="0" y="0" width="{S}" height="{S*0.62:.0f}" fill="url(#sheen)"/>'
             f'</g>')

    # forged center groove on handle + hairline highlight + recessed bolt hole
    hole_dark = hexagon(-250, 0, 72, rot=8)
    hole_lite = hexagon(-250 - 4, -4, 66, rot=8)
    groove = (f'<g transform="{tf}">'
              f'<line x1="-150" y1="2" x2="160" y2="2" stroke="{gen.IND_DEEP}" '
              f'stroke-opacity="0.55" stroke-width="20" stroke-linecap="round"/>'
              f'<line x1="-150" y1="-12" x2="150" y2="-12" stroke="{gen.IND_PALE}" '
              f'stroke-opacity="0.40" stroke-width="6" stroke-linecap="round"/>'
              f'<path d="{hole_dark}" fill="none" stroke="#1E1B4B" stroke-opacity="0.65" '
              f'stroke-width="12" stroke-linejoin="round"/>'
              f'<path d="{hole_lite}" fill="none" stroke="{gen.IND_PALE}" '
              f'stroke-opacity="0.45" stroke-width="5" stroke-linejoin="round"/>'
              f'</g>')

    # crisp darker edge for definition
    edge = (f'<path d="{d}" transform="{tf}" fill="none" stroke="#312E81" '
            f'stroke-opacity="0.55" stroke-width="4" '
            f'stroke-linejoin="round" stroke-linecap="round"/>')

    return extra_defs + base + gloss + groove + edge


def main():
    svg = gen.svg_wrap(inner())
    p = os.path.join(OUT, "wrench.svg")
    with open(p, "w") as f:
        f.write(svg)
    print("wrote", p)


if __name__ == "__main__":
    main()
