"""SVG renderer. Turns a Geometry into a clean, modern wiring diagram:
rounded bodies with subtle shadows, jumper-wire routing with white casing for
legible crossings, info cards, and a signal legend."""

from __future__ import annotations

import math
from pathlib import Path
from xml.sax.saxutils import escape

from .layout import Geometry, PlacedCard, PlacedPart, RoutedWire, wrap_card_text
from .theme import DEFAULT_THEME, Theme

PAD_W = 9.0
PAD_H = 6.0

# Raspberry Pi logo, as a single path in a 16x16 box (Font Awesome, CC BY 4.0).
_RPI_LOGO = (Path(__file__).parent / "assets" / "rpi_logo_path.txt").read_text().strip()


def render(geo: Geometry, theme: Theme = DEFAULT_THEME, grid: bool = False) -> str:
    s: list[str] = []
    s.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{geo.width:.0f}" height="{geo.height:.0f}" '
        f'viewBox="0 0 {geo.width:.0f} {geo.height:.0f}" '
        f'font-family="{theme.font}">'
    )
    s.append(_defs(theme))
    s.append(f'<rect width="{geo.width:.0f}" height="{geo.height:.0f}" '
             f'fill="{theme.bg}"/>')
    if grid:
        s.append(f'<rect width="{geo.width:.0f}" height="{geo.height:.0f}" '
                 f'fill="url(#wg-grid)"/>')

    # title
    if geo.title:
        s.append(f'<text x="48" y="44" font-size="22" font-weight="700" '
                 f'fill="{theme.title_color}">{escape(geo.title)}</text>')
    if geo.subtitle:
        s.append(f'<text x="48" y="66" font-size="13" '
                 f'fill="{theme.subtitle_color}">{escape(geo.subtitle)}</text>')

    # wires first (under the bodies' pin labels but the pads sit on top)
    for w in geo.wires:
        s.append(_wire(w, theme))

    for p in geo.parts:
        s.append(_part(p, theme))

    for c in geo.cards:
        s.append(_card(c, theme))

    s.append(_legend(geo, theme))
    s.append("</svg>")
    return "\n".join(x for x in s if x)


def _defs(theme: Theme) -> str:
    shadow = (
        '<filter id="soft" x="-20%" y="-20%" width="140%" height="140%">'
        '<feDropShadow dx="0" dy="3" stdDeviation="5" '
        'flood-color="#0f172a" flood-opacity="0.16"/></filter>'
    )
    g = theme.grid_size
    grid = (
        f'<pattern id="wg-grid" width="{g:.0f}" height="{g:.0f}" '
        f'patternUnits="userSpaceOnUse">'
        f'<path d="M {g:.0f} 0 L 0 0 0 {g:.0f}" fill="none" '
        f'stroke="{theme.grid_line}" stroke-width="1"/></pattern>'
    )
    return f"<defs>{shadow}{grid}</defs>"


# --- parts ------------------------------------------------------------------


def _part(p: PlacedPart, theme: Theme) -> str:
    if p.style == "pcb":
        return _part_pcb(p, theme)
    if p.style == "chip":
        return _part_chip(p, theme)
    return _part_card(p, theme)


def _darken(hex_color: str, factor: float) -> str:
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return hex_color
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    r, g, b = (int(c * factor) for c in (r, g, b))
    return f"#{r:02x}{g:02x}{b:02x}"


def _body_rect(box, fill, stroke, sw, r, shadow) -> str:
    filt = ' filter="url(#soft)"' if shadow else ""
    return (f'<rect x="{box.x:.1f}" y="{box.y:.1f}" width="{box.w:.1f}" '
            f'height="{box.h:.1f}" rx="{r}" fill="{fill}" stroke="{stroke}" '
            f'stroke-width="{sw}"{filt}/>')


def _label_lines(label: str) -> list[str]:
    """Split a trailing parenthetical onto its own line, e.g.
    'PCM1808 ADC (AliExpress)' -> ['PCM1808 ADC', '(AliExpress)']."""
    idx = label.find(" (")
    if idx != -1 and label.rstrip().endswith(")"):
        return [label[:idx].strip(), label[idx + 1:].strip()]
    return [label]


def _part_pcb(p: PlacedPart, theme: Theme) -> str:
    b = p.box
    fill = p.color or theme.pcb_fill
    stroke = _darken(p.color, 0.62) if p.color else theme.pcb_stroke
    out = [_body_rect(b, fill, stroke, theme.body_stroke_w,
                      theme.body_radius, theme.shadow)]
    # top-edge connector hint (e.g. USB), only when the part declares one. It
    # rides the part's original top edge, so it rotates onto whichever edge that
    # becomes after the turn.
    if p.connector:
        q = int(round(p.rotation / 90.0)) % 4
        body_h = b.w if q % 2 else b.h           # un-rotated body height
        ty = b.cy - body_h / 2
        bar = (f'<rect x="{b.cx-16:.1f}" y="{ty-7:.1f}" width="32" '
               f'height="10" rx="3" fill="#9ca3af" stroke="{stroke}"/>')
        if p.rotation:
            out.append(f'<g transform="rotate({p.rotation:.0f} {b.cx:.1f} '
                       f'{b.cy:.1f})">{bar}</g>')
        else:
            out.append(bar)
    # simplified on-board components, beneath the pins/labels
    out.append(_decorations(p, theme))
    has_top = any(pin.side == "top" for pin in p.pins)
    has_bottom = any(pin.side == "bottom" for pin in p.pins)
    lines = _label_lines(p.label)
    if has_top and has_bottom:
        # a quarter-turned dual-header board carries pin labels on both
        # horizontal edges, so float the identity on a small plate at the centre
        # where it can't collide with them
        tw = max((len(ln) for ln in lines), default=1) * 5.4 + 14
        th = 11 * len(lines) + 7
        out.append(f'<rect x="{b.cx-tw/2:.1f}" y="{b.cy-th/2:.1f}" '
                   f'width="{tw:.1f}" height="{th:.1f}" rx="4" fill="#1f2430" '
                   f'fill-opacity="0.85"/>')
        for i, ln in enumerate(lines):
            ly = b.cy - (len(lines) - 1) * 5.5 + i * 11 + 3
            out.append(f'<text x="{b.cx:.1f}" y="{ly:.1f}" text-anchor="middle" '
                       f'font-size="9" font-weight="700" '
                       f'fill="#ffffff">{escape(ln)}</text>')
    elif has_top:
        # top pins occupy the upper edge, so the identity sits on the board near
        # the bottom; a trailing "(...)" drops to its own line
        for i, ln in enumerate(reversed(lines)):
            ly = b.bottom - 8 - i * 11
            out.append(f'<text x="{b.cx:.1f}" y="{ly:.1f}" text-anchor="middle" '
                       f'font-size="9" font-weight="700" '
                       f'fill="#ffffff">{escape(ln)}</text>')
    else:
        out.append(f'<text x="{b.cx:.1f}" y="{b.y+18:.1f}" text-anchor="middle" '
                   f'font-size="9" font-weight="700" fill="{theme.pcb_title}">'
                   f'{escape(p.label)}</text>')
    for pin in p.pins:
        out.append(_pad(pin, theme.pcb_pad, theme.pcb_pad_stroke, pin.number))
        out.append(_pin_label(pin, theme.pcb_title, b))
    return "<g>" + "".join(out) + "</g>"


def _decorations(p: PlacedPart, theme: Theme) -> str:
    b = p.box
    q = int(round(p.rotation / 90.0)) % 4
    out: list[str] = []
    for d in p.part.decorations:
        # turn the normalised anchor with the board: (ax, ay) -> (1-ay, ax) per
        # clockwise quarter-turn, about the body centre (0.5, 0.5).
        ax, ay = d.at
        for _ in range(q):
            ax, ay = 1 - ay, ax
        cx = b.x + ax * b.w
        cy = b.y + ay * b.h
        g = _decoration_shape(d, cx, cy)
        rot = d.rot + p.rotation        # spin the shape itself to match the board
        if rot:
            out.append(f'<g transform="rotate({rot:.0f} {cx:.1f} {cy:.1f})">'
                       f'{g}</g>')
        else:
            out.append(g)
    return "".join(out)


def _decoration_shape(d, cx: float, cy: float) -> str:
    if d.type == "cap":
        r = d.r or 18.0
        # metallic can; left third darkened as a simple polarity mark
        cid = f"cap{int(cx)}_{int(cy)}"
        parts = [
            f'<clipPath id="{cid}"><circle cx="{cx:.1f}" cy="{cy:.1f}" '
            f'r="{r:.1f}"/></clipPath>',
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" fill="#b9bdc4"/>',
            f'<rect x="{cx-r:.1f}" y="{cy-r:.1f}" width="{2*r/3:.1f}" '
            f'height="{2*r:.1f}" fill="#3a3e45" clip-path="url(#{cid})"/>',
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" fill="none" '
            f'stroke="#b9bdc4" stroke-width="1.5"/>',
        ]
        if d.label:
            # spec lines printed on the can, left-aligned to the polarity edge
            lines = d.label.split()
            fs = max(5.5, r * 0.36)
            lh = fs * 1.12
            tx = cx - r / 3 + 1.5
            for i, ln in enumerate(lines):
                ty = cy + (i - (len(lines) - 1) / 2) * lh + fs * 0.35
                parts.append(
                    f'<text x="{tx:.1f}" y="{ty:.1f}" text-anchor="start" '
                    f'font-size="{fs:.1f}" font-weight="600" '
                    f'fill="#2f343a">{escape(ln)}</text>')
        return "".join(parts)
    if d.type == "resistor":
        w = d.w or 16.0
        h = d.h or 7.0
        x = cx - w / 2
        y = cy - h / 2
        cap = max(2.0, w * 0.18)
        return (
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" '
            f'rx="1" fill="#15181d" stroke="#0a0c0f" stroke-width="0.5"/>'
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{cap:.1f}" height="{h:.1f}" '
            f'fill="#b8bdc4"/>'
            f'<rect x="{x+w-cap:.1f}" y="{y:.1f}" width="{cap:.1f}" '
            f'height="{h:.1f}" fill="#b8bdc4"/>'
        )
    if d.type == "ic":
        w = d.w or 34.0
        h = d.h or 46.0
        x = cx - w / 2
        y = cy - h / 2
        s = [f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" '
             f'rx="2.5" fill="#16181d" stroke="#0a0c0f" stroke-width="1"/>']
        legs = 4
        for i in range(legs):
            ly = y + h * (i + 0.5) / legs
            s.append(f'<line x1="{x-3:.1f}" y1="{ly:.1f}" x2="{x:.1f}" '
                     f'y2="{ly:.1f}" stroke="#9aa0a6" stroke-width="1.4"/>')
            s.append(f'<line x1="{x+w:.1f}" y1="{ly:.1f}" x2="{x+w+3:.1f}" '
                     f'y2="{ly:.1f}" stroke="#9aa0a6" stroke-width="1.4"/>')
        s.append(f'<circle cx="{x+5:.1f}" cy="{y+5:.1f}" r="1.6" fill="#4b5563"/>')
        if d.label:
            s.append(f'<text x="{cx:.1f}" y="{cy+2:.1f}" text-anchor="middle" '
                     f'font-size="7" fill="#9aa0a6" '
                     f'transform="rotate(-90 {cx:.1f} {cy:.1f})">'
                     f'{escape(d.label)}</text>')
        return "".join(s)
    if d.type == "logo":
        # the real Raspberry Pi mark, in white silkscreen; the source path lives
        # in a 16x16 box, so scale by r/8 (r = half-size) and centre on (cx, cy)
        s = d.r or 16.0
        k = s / 8.0
        tx = cx - 8 * k
        ty = cy - 8 * k
        return (f'<g transform="translate({tx:.2f} {ty:.2f}) scale({k:.4f})">'
                f'<path d="{_RPI_LOGO}" fill="#eef5f0"/></g>')
    if d.type == "button":
        w = d.w or 28.0
        h = d.h or 16.0
        x = cx - w / 2
        y = cy - h / 2
        ar = min(w, h) * 0.34          # round actuator in the middle
        s = [
            # dark SMD housing
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" '
            f'rx="2.5" fill="#2b2f36" stroke="#0a0c0f" stroke-width="0.8"/>',
            # silver actuator
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{ar:.1f}" fill="#c7ccd1" '
            f'stroke="#8b9197" stroke-width="1"/>',
        ]
        if d.label:
            # white silkscreen caption below the button
            fs = max(5.5, h * 0.42)
            s.append(f'<text x="{cx:.1f}" y="{y+h+fs+1:.1f}" '
                     f'text-anchor="middle" font-size="{fs:.1f}" '
                     f'font-weight="700" fill="#eaf2ec">{escape(d.label)}</text>')
        return "".join(s)
    if d.type == "hole":
        r = d.r or 8.0
        # plated mounting hole: light annular pad with a dark bore. A coloured
        # hole (e.g. a white/red RCA jack face) recolours the ring and darkens
        # its rim to match.
        face = d.color or "#c7ccd1"
        rim = _darken(d.color, 0.6) if d.color else "#8b9197"
        return (
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" fill="{face}" '
            f'stroke="{rim}" stroke-width="1"/>'
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r*0.52:.1f}" '
            f'fill="#2c2630"/>'
        )
    # dot
    r = d.r or 3.0
    return (f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" '
            f'fill="{d.color or "#4b5563"}"/>')


def _part_chip(p: PlacedPart, theme: Theme) -> str:
    b = p.box
    out = [_body_rect(b, theme.chip_fill, theme.chip_fill, theme.body_stroke_w,
                      theme.body_radius, theme.shadow)]
    # pin-1 dot, top-left
    out.append(f'<circle cx="{b.x+13:.1f}" cy="{b.y+13:.1f}" r="3" '
               f'fill="#94a3b8"/>')
    out.append(f'<text x="{b.cx:.1f}" y="{b.y+20:.1f}" text-anchor="middle" '
               f'font-size="13" font-weight="700" fill="{theme.chip_title}">'
               f'{escape(p.label)}</text>')
    for pin in p.pins:
        out.append(_pad(pin, theme.chip_pad, theme.chip_pad, pin.number))
        out.append(_pin_label(pin, theme.chip_title, b))
    return "<g>" + "".join(out) + "</g>"


def _part_card(p: PlacedPart, theme: Theme) -> str:
    b = p.box
    out = [_body_rect(b, theme.module_fill, theme.body_stroke, theme.body_stroke_w,
                      theme.body_radius, theme.shadow)]
    # accent strip under the title
    out.append(f'<rect x="{b.x:.1f}" y="{b.y:.1f}" width="{b.w:.1f}" height="28" '
               f'rx="{theme.body_radius}" fill="{theme.module_accent}"/>')
    out.append(f'<rect x="{b.x:.1f}" y="{b.y+14:.1f}" width="{b.w:.1f}" '
               f'height="14" fill="{theme.module_accent}"/>')
    out.append(f'<text x="{b.cx:.1f}" y="{b.y+19:.1f}" text-anchor="middle" '
               f'font-size="12.5" font-weight="700" fill="#ffffff">'
               f'{escape(p.label)}</text>')
    for pin in p.pins:
        out.append(_pad(pin, theme.module_pad, theme.module_pad_stroke,
                        pin.number))
        out.append(_pin_label(pin, theme.pin_label, b))
    return "<g>" + "".join(out) + "</g>"


NUMPAD_W = 16.0          # enlarged side pad that carries the physical pin number
NUMPAD_H = 11.0


def _pad(pin, fill, stroke, number=None, num_color="#3f3f46") -> str:
    # A numbered pin gets an enlarged pad printed with its number, sitting mostly
    # outside the board edge so the wire lands on it and emerges from its outer
    # edge — the number is on opaque copper, never crossed by a wire. The long
    # axis of the pad runs along the body edge, so it follows part rotation.
    if number:
        if pin.side in ("left", "right"):
            w, h = NUMPAD_W, NUMPAD_H
            x = pin.x - (NUMPAD_W - 4) if pin.side == "left" else pin.x - 4
            y = pin.y - NUMPAD_H / 2
        else:  # top / bottom: the enlarged pad extends along the vertical
            w, h = NUMPAD_H, NUMPAD_W
            x = pin.x - NUMPAD_H / 2
            y = pin.y - (NUMPAD_W - 4) if pin.side == "top" else pin.y - 4
        return (f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" '
                f'height="{h:.1f}" rx="2.5" fill="{fill}" stroke="{stroke}" '
                f'stroke-width="1"/>'
                f'<text x="{x+w/2:.1f}" y="{y+h/2+3:.1f}" '
                f'text-anchor="middle" font-size="8" font-weight="600" '
                f'fill="{num_color}">{escape(number)}</text>')
    if pin.side in ("left", "right"):
        x = pin.x - PAD_W / 2
        y = pin.y - PAD_H / 2
        w, h = PAD_W, PAD_H
    else:
        x = pin.x - PAD_H / 2
        y = pin.y - PAD_W / 2
        w, h = PAD_H, PAD_W
    return (f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" '
            f'rx="1.5" fill="{fill}" stroke="{stroke}" stroke-width="1"/>')


def _pin_label(pin, label_color, box) -> str:
    # the physical pin number now lives on the pad itself; this draws the name
    out = []
    if pin.side == "left":
        out.append(f'<text x="{box.x+9:.1f}" y="{pin.y+3.5:.1f}" '
                   f'text-anchor="start" font-size="10.5" '
                   f'fill="{label_color}">{escape(pin.text)}</text>')
    elif pin.side == "right":
        out.append(f'<text x="{box.right-9:.1f}" y="{pin.y+3.5:.1f}" '
                   f'text-anchor="end" font-size="10.5" '
                   f'fill="{label_color}">{escape(pin.text)}</text>')
    else:  # top / bottom: gap to the pad matches the side pins' inset
        if pin.side == "top":
            y = box.y + 16        # pad bottom (+4.5) + 4.5 gap + cap height
        else:
            y = box.bottom - 9    # 4.5 gap above the pad, no descenders
        out.append(f'<text x="{pin.x:.1f}" y="{y:.1f}" '
                   f'text-anchor="middle" font-size="10.5" '
                   f'fill="{label_color}">{escape(pin.text)}</text>')
    return "".join(out)


# --- wires ------------------------------------------------------------------


def _wire(w: RoutedWire, theme: Theme) -> str:
    d = _rounded_path(w.points, theme.wire_radius)
    casing = (f'<path d="{d}" fill="none" stroke="{theme.wire_casing}" '
              f'stroke-width="{theme.wire_casing_w}" stroke-linecap="round" '
              f'stroke-linejoin="round"/>')
    line = (f'<path d="{d}" fill="none" stroke="{w.color}" '
            f'stroke-width="{theme.wire_w}" stroke-linecap="round" '
            f'stroke-linejoin="round"/>')
    dots = ""
    if w.points:
        a = w.points[0]
        b = w.points[-1]
        for (px, py) in (a, b):
            dots += (f'<circle cx="{px:.1f}" cy="{py:.1f}" '
                     f'r="{theme.endpoint_r}" fill="{w.color}" '
                     f'stroke="{theme.wire_casing}" stroke-width="1.5"/>')
    return casing + line + dots


def _rounded_path(points, r: float) -> str:
    if len(points) < 2:
        return ""
    d = [f"M {points[0][0]:.1f} {points[0][1]:.1f}"]
    for i in range(1, len(points) - 1):
        p0, p1, p2 = points[i - 1], points[i], points[i + 1]
        a = _shorten(p1, p0, r)
        b = _shorten(p1, p2, r)
        d.append(f"L {a[0]:.1f} {a[1]:.1f}")
        d.append(f"Q {p1[0]:.1f} {p1[1]:.1f} {b[0]:.1f} {b[1]:.1f}")
    d.append(f"L {points[-1][0]:.1f} {points[-1][1]:.1f}")
    return " ".join(d)


def _shorten(p, toward, r):
    dx, dy = toward[0] - p[0], toward[1] - p[1]
    L = math.hypot(dx, dy)
    if L == 0:
        return p
    rr = min(r, L / 2)
    return (p[0] + dx / L * rr, p[1] + dy / L * rr)


# --- cards & legend ---------------------------------------------------------


def _card(c: PlacedCard, theme: Theme) -> str:
    b = c.box
    out = [f'<rect x="{b.x:.1f}" y="{b.y:.1f}" width="{b.w:.1f}" '
           f'height="{b.h:.1f}" rx="{theme.card_radius}" fill="{theme.card_fill}" '
           f'stroke="{theme.card_stroke}" stroke-width="1.5" filter="url(#soft)"/>']
    out.append(f'<path d="{_top_round(b, theme.card_radius)}" '
               f'fill="{theme.card_title_bg}"/>')
    out.append(f'<text x="{b.x+14:.1f}" y="{b.y+19:.1f}" font-size="12.5" '
               f'font-weight="700" fill="{theme.card_title_fg}">'
               f'{escape(c.title)}</text>')
    y = b.y + 30 + 18
    for k, v in c.rows.items():
        out.append(f'<text x="{b.x+14:.1f}" y="{y:.1f}" font-size="11" '
                   f'fill="{theme.card_key}">{escape(k)}</text>')
        out.append(f'<text x="{b.right-14:.1f}" y="{y:.1f}" text-anchor="end" '
                   f'font-size="11" font-weight="600" fill="{theme.card_value}">'
                   f'{escape(v)}</text>')
        y += 24
    if c.text:
        for line in wrap_card_text(c.text):
            if line:
                out.append(f'<text x="{b.x+14:.1f}" y="{y:.1f}" font-size="10.5" '
                           f'fill="{theme.card_key}">{escape(line)}</text>')
            y += 15
    return "<g>" + "".join(out) + "</g>"


def _top_round(b, r) -> str:
    return (f"M {b.x:.1f} {b.y+30:.1f} L {b.x:.1f} {b.y+r:.1f} "
            f"Q {b.x:.1f} {b.y:.1f} {b.x+r:.1f} {b.y:.1f} "
            f"L {b.right-r:.1f} {b.y:.1f} "
            f"Q {b.right:.1f} {b.y:.1f} {b.right:.1f} {b.y+r:.1f} "
            f"L {b.right:.1f} {b.y+30:.1f} Z")


def _legend(geo: Geometry, theme: Theme) -> str:
    if not geo.legend:
        return ""
    y = geo.height - 38
    items = geo.legend
    w = 24 + sum(70 + len(lbl) * 6 for lbl, _ in items)
    # centre the legend bar horizontally in the canvas
    x = (geo.width - w) / 2 + 12
    out = [f'<rect x="{x-12:.1f}" y="{y-20:.1f}" width="{w:.0f}" height="34" '
           f'rx="9" fill="{theme.legend_fill}" stroke="{theme.legend_stroke}" '
           f'stroke-width="1.5"/>']
    cx = x
    for lbl, color in items:
        out.append(f'<line x1="{cx:.1f}" y1="{y-2:.1f}" x2="{cx+22:.1f}" '
                   f'y2="{y-2:.1f}" stroke="{color}" stroke-width="3.5" '
                   f'stroke-linecap="round"/>')
        out.append(f'<text x="{cx+28:.1f}" y="{y+2:.1f}" font-size="11" '
                   f'fill="{theme.card_value}">{escape(lbl)}</text>')
        cx += 70 + len(lbl) * 6
    return "<g>" + "".join(out) + "</g>"
