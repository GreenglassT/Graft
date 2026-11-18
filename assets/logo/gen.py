#!/usr/bin/env python3
"""Generate Graft logo concepts (wrench + signature, glossy indigo)."""
import math, os

S = 1024
OUT = os.path.dirname(os.path.abspath(__file__))

# Palette (from Graft DesignSystem/Palette.swift)
BG_TOP   = "#1E293B"
BG_BOT   = "#0F172A"
LINE     = "#334155"
ACCENT   = "#818CF8"
IND_LITE = "#A5B4FC"
IND_DEEP = "#6366F1"
IND_PALE = "#C7D2FE"
TEXT     = "#F8FAFC"


def polar(cx, cy, r, deg):
    a = math.radians(deg)
    return (cx + r * math.cos(a), cy + r * math.sin(a))


def ring_with_gap(cx, cy, R, r, gap_center, gap_w, steps=80):
    """Open-end wrench head: annulus with a wedge gap (the jaw)."""
    start = gap_center + gap_w / 2
    end = gap_center + 360 - gap_w / 2
    pts = []
    for i in range(steps + 1):
        t = start + (end - start) * i / steps
        pts.append(polar(cx, cy, R, t))
    for i in range(steps + 1):
        t = end + (start - end) * i / steps
        pts.append(polar(cx, cy, r, t))
    d = "M %.2f %.2f " % pts[0]
    d += " ".join("L %.2f %.2f" % p for p in pts[1:])
    d += " Z"
    return d


def wrench(cx, cy, R=150, r=80, shaft_half=58, shaft_len=320,
           gap_center=270, gap_w=92, fill="url(#ind)", grp_id="w"):
    """Open-end wrench pointing 'up' (gap up), shaft going down. Caller rotates."""
    head = ring_with_gap(cx, cy, R, r, gap_center, gap_w)
    # shaft: stadium tucked under the ring
    x0, x1 = cx - shaft_half, cx + shaft_half
    y0, y1 = cy, cy + shaft_len
    shaft = (f"M {x0:.1f} {y0:.1f} L {x0:.1f} {y1-shaft_half:.1f} "
             f"A {shaft_half:.1f} {shaft_half:.1f} 0 0 0 {x1:.1f} {y1-shaft_half:.1f} "
             f"L {x1:.1f} {y0:.1f} Z")
    return (f'<g id="{grp_id}">'
            f'<path d="{shaft}" fill="{fill}"/>'
            f'<path d="{head}" fill="{fill}"/>'
            f'</g>')


def fountain_pen(cx, cy, length=460, w=70, fill="url(#pen)"):
    """A pen pointing up-ish (nib at top). Caller rotates. Drawn vertical, nib up."""
    half = w / 2
    nib = length * 0.30
    bx0, bx1 = cx - half, cx + half
    by0 = cy - length / 2 + nib          # body top
    by1 = cy + length / 2                 # body bottom
    # nib triangle at top
    nib_top = (cx, cy - length / 2)
    body = (f"M {bx0:.1f} {by0:.1f} "
            f"L {bx1:.1f} {by0:.1f} "
            f"L {bx1:.1f} {by1-half:.1f} "
            f"A {half:.1f} {half:.1f} 0 0 1 {bx0:.1f} {by1-half:.1f} Z")
    nibp = (f"M {bx0:.1f} {by0:.1f} L {nib_top[0]:.1f} {nib_top[1]:.1f} "
            f"L {bx1:.1f} {by0:.1f} Z")
    slit = (f'<line x1="{cx:.1f}" y1="{by0:.1f}" x2="{cx:.1f}" y2="{nib_top[1]+nib*0.45:.1f}" '
            f'stroke="{BG_BOT}" stroke-width="8" stroke-linecap="round"/>')
    return (f'<g>'
            f'<path d="{body}" fill="{fill}"/>'
            f'<path d="{nibp}" fill="{IND_PALE}"/>'
            f'{slit}'
            f'</g>')


def defs():
    return f'''<defs>
  <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="{BG_TOP}"/>
    <stop offset="1" stop-color="{BG_BOT}"/>
  </linearGradient>
  <radialGradient id="glow" cx="0.5" cy="0.32" r="0.7">
    <stop offset="0" stop-color="{ACCENT}" stop-opacity="0.30"/>
    <stop offset="1" stop-color="{ACCENT}" stop-opacity="0"/>
  </radialGradient>
  <linearGradient id="ind" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="{IND_LITE}"/>
    <stop offset="1" stop-color="{IND_DEEP}"/>
  </linearGradient>
  <linearGradient id="pen" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="{IND_PALE}"/>
    <stop offset="1" stop-color="{ACCENT}"/>
  </linearGradient>
  <linearGradient id="gloss" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#FFFFFF" stop-opacity="0.16"/>
    <stop offset="0.5" stop-color="#FFFFFF" stop-opacity="0"/>
  </linearGradient>
  <filter id="soft" x="-30%" y="-30%" width="160%" height="160%">
    <feGaussianBlur stdDeviation="10"/>
  </filter>
  <filter id="ds" x="-40%" y="-40%" width="180%" height="180%">
    <feDropShadow dx="0" dy="10" stdDeviation="14" flood-color="#000000" flood-opacity="0.45"/>
  </filter>
</defs>'''


def background(corner=229):
    return (f'<rect x="0" y="0" width="{S}" height="{S}" rx="{corner}" fill="url(#bg)"/>'
            f'<rect x="0" y="0" width="{S}" height="{S}" rx="{corner}" fill="url(#glow)"/>'
            f'<rect x="0" y="0" width="{S}" height="{S/2}" rx="{corner}" fill="url(#gloss)"/>'
            f'<rect x="3" y="3" width="{S-6}" height="{S-6}" rx="{corner-3}" '
            f'fill="none" stroke="{LINE}" stroke-width="3" stroke-opacity="0.7"/>')


def svg_wrap(inner, corner=229):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{S}" height="{S}" '
            f'viewBox="0 0 {S} {S}">{defs()}'
            f'<clipPath id="clip"><rect width="{S}" height="{S}" rx="{corner}"/></clipPath>'
            f'<g clip-path="url(#clip)">{background(corner)}{inner}</g></svg>')


def signature(d, stroke=IND_PALE, width=30, glow=True):
    g = ''
    if glow:
        g = (f'<path d="{d}" fill="none" stroke="{ACCENT}" stroke-opacity="0.55" '
             f'stroke-width="{width+14}" stroke-linecap="round" stroke-linejoin="round" '
             f'filter="url(#soft)"/>')
    return (g + f'<path d="{d}" fill="none" stroke="{stroke}" stroke-width="{width}" '
            f'stroke-linecap="round" stroke-linejoin="round"/>')


# ---------------- Concept A: wrench + signature underline ----------------
def concept_A():
    w = wrench(512, 470, R=150, r=80, shaft_half=58, shaft_len=300, gap_w=92)
    wg = f'<g transform="rotate(-38 512 512)" filter="url(#ds)">{w}</g>'
    sig_d = ("M 300 700 C 360 600, 430 760, 500 660 "
             "S 600 600, 660 700 L 720 640 L 760 690 L 830 590")
    return svg_wrap(wg + signature(sig_d, width=28))


# ---------------- Concept B: crossed wrench + pen emblem ----------------
def concept_B():
    w = wrench(512, 512, R=140, r=74, shaft_half=54, shaft_len=300, gap_w=92)
    wg = f'<g transform="rotate(48 512 512) translate(0 -40)" filter="url(#ds)">{w}</g>'
    pen = fountain_pen(512, 512, length=470, w=68)
    pg = f'<g transform="rotate(-48 512 512) translate(0 -40)" filter="url(#ds)">{pen}</g>'
    sig_d = "M 320 760 C 430 700, 600 700, 700 750"
    return svg_wrap(wg + pg + signature(sig_d, width=22))


# ---------------- Concept C: wrench-as-checkmark ----------------
def concept_C():
    # Wrench forms the long arm of a check; short stub forms the tick.
    w = wrench(512, 470, R=140, r=74, shaft_half=56, shaft_len=300, gap_w=92)
    wg = f'<g transform="rotate(40 512 512) translate(60 0)" filter="url(#ds)">{w}</g>'
    # short tick of the checkmark, indigo, leading into the wrench handle
    tick = ('<path d="M 300 540 L 410 660" fill="none" stroke="url(#ind)" '
            'stroke-width="92" stroke-linecap="round"/>')
    sig_d = "M 300 770 C 430 720, 620 720, 740 770"
    return svg_wrap(tick + wg + signature(sig_d, width=22))


def main():
    for name, fn in [("A", concept_A), ("B", concept_B), ("C", concept_C)]:
        path = os.path.join(OUT, f"icon_{name}.svg")
        with open(path, "w") as f:
            f.write(fn())
        print("wrote", path)


if __name__ == "__main__":
    main()
