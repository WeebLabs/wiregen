"""Hint-driven layout. The author supplies coarse intent (region, order, flip);
this module owns every coordinate: part sizes, pin positions, channel routing,
card placement, and the final canvas bounds. No pixel bookkeeping upstream.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .connections import expand
from .library import Library
from .model import Diagram, PartDef, PartInstance
from .standards import HUE_LABEL, get_bus, wire_color, wire_hue
from .theme import DEFAULT_THEME, Theme

# tunables (px)
PIN_PITCH = 24.0
PAD_TOP = 34.0
PAD_BOTTOM = 16.0
MIN_BODY_H = 70.0
DEFAULT_W = 150.0
CHANNEL = 240.0
VGAP = 64.0
STUB = 18.0
TRACK_GAP = 19.0        # minimum elbow spacing
TRACK_GAP_MAX = 36.0    # cap on elbow spacing when spreading
LANE_GAP = 18.0
WRAP_GAP = 12.0
WRAP_CLEAR = 30.0       # wrap clearance for parts with pin numbers
WRAP_CLEAR_MIN = 13.0   # wrap clearance for parts with nothing protruding
TRACK_MARGIN = 30.0     # keep elbow tracks this far inside the channel edges
TOP = 48.0 + 54.0 + 30.0   # margin + title band + lane band
LEFT = 48.0
CARD_W = 236.0
CARD_GAP = 24.0
ROW_H = 24.0
CARD_PAD = 14.0         # inner horizontal padding for card content
TEXT_LH = 15.0          # line height for wrapped free-text notes
FIRST_ROW = 48.0        # first content baseline below the card top (30 band + 18)
_TEXT_CHAR_W = 5.05     # ~avg glyph width at the 10.5px note font, for wrapping


def wrap_card_text(text: str) -> list[str]:
    """Greedy word-wrap a free-text note to the card's inner width. Newlines in
    the source separate paragraphs, rendered with a blank line between them."""
    max_chars = max(1, int((CARD_W - 2 * CARD_PAD) / _TEXT_CHAR_W))
    lines: list[str] = []
    paragraphs = [p for p in text.split("\n") if p.strip()]
    for pi, para in enumerate(paragraphs):
        if pi > 0:
            lines.append("")        # blank line between paragraphs
        cur = ""
        for word in para.split():
            cand = f"{cur} {word}".strip()
            if not cur or len(cand) <= max_chars:
                cur = cand
            else:
                lines.append(cur)
                cur = word
        if cur:
            lines.append(cur)
    return lines


@dataclass
class Box:
    x: float
    y: float
    w: float
    h: float

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def bottom(self) -> float:
        return self.y + self.h

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2


@dataclass
class PlacedPin:
    name: str
    text: str
    number: str | None
    net: str | None
    signal: str | None
    role: str | None
    x: float
    y: float
    side: str  # effective side after flip


@dataclass
class PlacedPart:
    iid: str
    part: PartDef
    label: str
    box: Box
    pins: list[PlacedPin]
    style: str
    package: str
    color: str | None = None
    connector: str | None = None
    rotation: float = 0.0   # clockwise degrees the part was turned (0/90/180/270)


@dataclass
class RoutedWire:
    points: list[tuple[float, float]]
    color: str
    label: str | None


@dataclass
class PlacedCard:
    box: Box
    title: str
    rows: dict[str, str]
    text: str | None


@dataclass
class Geometry:
    parts: list[PlacedPart]
    wires: list[RoutedWire]
    cards: list[PlacedCard]
    legend: list[tuple[str, str]]
    width: float
    height: float
    title: str | None
    subtitle: str | None


def _eff_side(side: str, flip: bool) -> str:
    if not flip:
        return side
    return {"left": "right", "right": "left"}.get(side, side)


# --- rotation (orthogonal, clockwise) ---------------------------------------
# A turn of the whole part is expressed as a number of 90 deg clockwise
# quarter-turns. We place pins on the un-rotated body, then rotate each pin's
# coordinate and its side; this keeps labels horizontal and wiring intact.

_CW_SIDE = {"left": "top", "top": "right", "right": "bottom", "bottom": "left"}


def _quarter(rotate: float) -> int:
    return int(round(rotate / 90.0)) % 4


def _rot_side(side: str, q: int) -> str:
    for _ in range(q):
        side = _CW_SIDE[side]
    return side


def _rot_point(x: float, y: float, cx: float, cy: float,
               q: int) -> tuple[float, float]:
    """Rotate (x, y) about (cx, cy) by ``q`` clockwise quarter-turns. SVG's
    y-axis points down, so a clockwise turn maps (dx, dy) -> (-dy, dx)."""
    for _ in range(q):
        dx, dy = x - cx, y - cy
        x, y = cx - dy, cy + dx
    return x, y


def _footprint(w: float, h: float, q: int) -> tuple[float, float]:
    """On-canvas bounding box after the turn: width/height swap for 90/270."""
    return (h, w) if q % 2 else (w, h)


def _placed_candidates(part: PlacedPart, token: str) -> list[PlacedPin]:
    t = token.strip().lower()
    for key in ("name", "text", "net"):
        hits = [p for p in part.pins
                if (getattr(p, key) or "").lower() == t]
        if hits:
            return hits
    return []


def _body_size(part: PartDef, inst: PartInstance) -> tuple[float, float]:
    left_n = sum(1 for p in part.pins if _eff_side(p.side, inst.flip) == "left")
    right_n = sum(1 for p in part.pins if _eff_side(p.side, inst.flip) == "right")
    rows = max(left_n, right_n, 1)
    computed_h = max(MIN_BODY_H, rows * PIN_PITCH + PAD_TOP + PAD_BOTTOM)
    # honour an explicit height override, but never shrink below what the pins
    # need (would crowd them)
    h = max(computed_h, float(part.height)) if part.height else computed_h
    w = inst.width if getattr(inst, "width", None) else (part.width or DEFAULT_W)
    return float(w), float(h)


def _place_pins(part: PartDef, inst: PartInstance, box: Box) -> list[PlacedPin]:
    sides: dict[str, list] = {"left": [], "right": [], "top": [], "bottom": []}
    for p in part.pins:
        sides[_eff_side(p.side, inst.flip)].append(p)
    placed: list[PlacedPin] = []
    # all pins use the same fixed pitch (matching every board); a taller body
    # just gains margin. Side pins centre vertically in the padded region;
    # top/bottom pins centre on the body width.
    pitch = PIN_PITCH
    for side, pins in sides.items():
        n = len(pins)
        if n == 0:
            continue
        if side in ("left", "right"):
            x = box.x if side == "left" else box.right
            center = box.y + box.h / 2   # true body centre, not the padded region
            for i, p in enumerate(pins):
                y = center + (i - (n - 1) / 2) * pitch
                placed.append(PlacedPin(p.name, p.text, p.number, p.net,
                                        p.signal, p.role, x, y, side))
        else:
            y = box.y if side == "top" else box.bottom
            start = box.cx - (n - 1) / 2 * pitch
            for i, p in enumerate(pins):
                x = start + i * pitch
                placed.append(PlacedPin(p.name, p.text, p.number, p.net,
                                        p.signal, p.role, x, y, side))
    return placed


def _place_pins_rotated(part: PartDef, inst: PartInstance,
                        box: Box) -> list[PlacedPin]:
    """Place pins honouring ``inst.rotate``. ``box`` is the on-canvas footprint
    (already width/height-swapped for 90/270). We lay the pins out on the
    un-rotated body about the same centre, then rotate every pin's coordinate
    and side, so they land exactly on the footprint's edges."""
    q = _quarter(inst.rotate)
    if q == 0:
        return _place_pins(part, inst, box)
    w0, h0 = _body_size(part, inst)        # un-rotated body size
    cx, cy = box.cx, box.cy
    tmp = Box(cx - w0 / 2, cy - h0 / 2, w0, h0)
    rotated: list[PlacedPin] = []
    for pin in _place_pins(part, inst, tmp):
        x, y = _rot_point(pin.x, pin.y, cx, cy, q)
        rotated.append(PlacedPin(pin.name, pin.text, pin.number, pin.net,
                                 pin.signal, pin.role, x, y,
                                 _rot_side(pin.side, q)))
    return rotated


def build(diagram: Diagram, lib: Library, theme: Theme = DEFAULT_THEME) -> Geometry:
    # 1. size every instance
    sized: dict[str, tuple[PartDef, PartInstance, float, float]] = {}
    for iid, inst in diagram.parts.items():
        pdef = lib.get(inst.part)
        w0, h0 = _body_size(pdef, inst)
        w, h = _footprint(w0, h0, _quarter(inst.rotate))
        sized[iid] = (pdef, inst, w, h)

    # 2. group into region columns, ordered within each
    regions: dict[str, list[str]] = {"left": [], "center": [], "right": []}
    for iid, inst in diagram.parts.items():
        regions[inst.region].append(iid)
    for r in regions:
        regions[r].sort(key=lambda iid: (
            diagram.parts[iid].order if diagram.parts[iid].order is not None
            else list(diagram.parts).index(iid)))

    present = [r for r in ("left", "center", "right") if regions[r]]
    colw = {r: max(sized[i][2] for i in regions[r]) for r in present}
    colx: dict[str, float] = {}
    cursor = LEFT
    for r in present:
        colx[r] = cursor
        cursor += colw[r] + CHANNEL

    # vertical: centre each column around a common mid
    colh = {r: sum(sized[i][3] for i in regions[r])
            + VGAP * (len(regions[r]) - 1) for r in present}
    max_h = max(colh.values()) if colh else 0.0
    mid = TOP + max_h / 2

    placed: dict[str, PlacedPart] = {}
    for r in present:
        y = mid - colh[r] / 2
        for iid in regions[r]:
            pdef, inst, w, h = sized[iid]
            x = colx[r] + (colw[r] - w) / 2
            box = Box(x + inst.offset_x, y + inst.offset_y, w, h)
            pins = _place_pins_rotated(pdef, inst, box)
            placed[iid] = PlacedPart(iid, pdef, inst.label or pdef.name,
                                     box, pins, pdef.style or "card",
                                     pdef.package, pdef.color, pdef.connector,
                                     rotation=float(inst.rotate))
            y += h + VGAP

    parts_top = min((p.box.y for p in placed.values()), default=TOP)
    parts_bottom = max((p.box.bottom for p in placed.values()), default=TOP)
    # wrap clearance per part: hug the edge unless pin numbers protrude
    part_clear = {id(p.box): (WRAP_CLEAR if any(pin.number for pin in p.pins)
                              else WRAP_CLEAR_MIN) for p in placed.values()}

    # 3. resolve + route wires
    wires: list[RoutedWire] = []
    legend: dict[str, str] = {}
    specs = expand(diagram)

    # pre-resolve endpoints to placed pins (with proximity for duplicates)
    resolved: list[tuple[PlacedPin, PlacedPin, str, str | None, str | None]] = []
    for w in specs:
        pa = placed.get(w.a.instance)
        pb = placed.get(w.b.instance)
        if not pa or not pb:
            continue
        ca = _placed_candidates(pa, w.a.token)
        cb = _placed_candidates(pb, w.b.token)
        if not ca or not cb:
            continue
        # when an endpoint name is ambiguous (e.g. several 3V3 pads) and the
        # author asked to detour a given side, prefer a candidate already on
        # that edge so the wire reaches its lane without a needless traversal
        if w.route in ("top", "bottom"):
            ca = [p for p in ca if p.side == w.route] or ca
            cb = [p for p in cb if p.side == w.route] or cb
        pin_a, pin_b = _nearest_pair(ca, cb)
        net = pin_a.net or pin_b.net
        sig = w.signal or pin_a.signal or pin_b.signal
        role = pin_a.role or pin_b.role
        color = wire_color(sig, role, net, w.color, theme)
        resolved.append((pin_a, pin_b, color, w.label, w.route))
        # legend entry: nets first, then per-role / per-bus signal lines
        bus = get_bus(w.bus or sig)
        if net and net.lower() in ("gnd", "agnd"):
            legend.setdefault("GND", color)
        elif net and net.lower() in ("3v3",):
            legend.setdefault("3V3", color)
        elif net and net.lower() in ("5v", "vbus", "vcc", "vin"):
            legend.setdefault("5V / VIN", color)
        elif bus:
            hue = wire_hue(sig, role)
            sub = HUE_LABEL.get(hue or "")
            legend.setdefault(f"{bus.label} {sub}" if sub else bus.label, color)

    # classify every wire: jumper (same edge), wrap (a detour), or channel
    items = []
    for pa_pin, pb_pin, color, label, route in resolved:
        box_a = _owner_box(placed, pa_pin)
        box_b = _owner_box(placed, pb_pin)
        if box_a is box_b and pa_pin.side == pb_pin.side:
            kind = "jumper"
        elif (route is not None
              or _needs_wrap(pa_pin, box_a, box_b)
              or _needs_wrap(pb_pin, box_b, box_a)):
            kind = "wrap"
        else:
            kind = "channel"
        items.append([pa_pin, pb_pin, color, label, route, box_a, box_b, kind])

    # channel wires get elbow tracks. Straight wires (no vertical run) need
    # none. The rest are packed: wires whose vertical spans don't overlap can
    # share one elbow column, so e.g. an up-going wire aligns with a disjoint
    # down-going one instead of claiming its own track.
    chan = [it for it in items if it[7] == "channel"]
    straight_eps = 1.5

    def _cc(it):
        a, b = it[5], it[6]
        lo, hi = (a, b) if a.cx <= b.cx else (b, a)
        return (lo.right + hi.x) / 2

    elbow = [it for it in chan if abs(it[0].y - it[1].y) > straight_eps]
    # nested-fan order: wires to lower endpoints take outer (earlier) tracks
    elbow.sort(key=lambda it: -max(it[0].y, it[1].y))
    tracks = []                      # each track holds the (y0, y1) spans on it
    track_of = {}
    for it in elbow:
        y0, y1 = sorted((it[0].y, it[1].y))
        chosen = None
        for t in range(len(tracks) - 1, -1, -1):   # prefer the rightmost free track
            if all(y1 < s0 or y0 > s1 for s0, s1 in tracks[t]):
                chosen = t
                break
        if chosen is None:
            chosen = len(tracks)
            tracks.append([])
        tracks[chosen].append((y0, y1))
        track_of[id(it)] = chosen
    # space the tracks out for readability: widen toward TRACK_GAP_MAX as the
    # channel allows, then centre the band (wires sharing a track move together)
    n_tracks = len(tracks)
    for it in elbow:
        a, b = it[5], it[6]
        lo, hi = (a, b) if a.cx <= b.cx else (b, a)
        cc = (lo.right + hi.x) / 2
        if n_tracks <= 1:
            x = cc
        else:
            avail = (hi.x - TRACK_MARGIN) - (lo.right + TRACK_MARGIN)
            gap = min(TRACK_GAP_MAX, max(TRACK_GAP, avail / (n_tracks - 1)))
            x = cc + (track_of[id(it)] - (n_tracks - 1) / 2) * gap
        it.append(x)
    for it in chan:
        if len(it) == 8:            # straight wire: centre track -> flat run
            it.append(_cc(it))

    # crossing-aware wrap planning: choose each wrap's leg offsets and lane
    # height so a fan never self-crosses, wider wraps ride outside narrower ones,
    # and each lane only clears the boards actually beneath it (see _plan_wraps).
    boxes = [p.box for p in placed.values()]
    wrap_plan = _plan_wraps(items, boxes, part_clear, parts_top, parts_bottom)

    for it in items:
        pa_pin, pb_pin, color, label, route, box_a, box_b, kind = it[:8]
        if kind == "jumper":
            dist = abs(pa_pin.y - pb_pin.y) + abs(pa_pin.x - pb_pin.x)
            pts = _side_jumper(pa_pin, pb_pin, box_a, 12 + dist * 0.16)
            wires.append(RoutedWire(_clean(pts), color, label))
            continue
        if kind == "channel":
            track_x = it[8]
            appr_a = _approach(pa_pin, box_a, box_b, track_x, 0)
            appr_b = _approach(pb_pin, box_b, box_a, track_x, 0)
            wires.append(RoutedWire(_clean(appr_a + list(reversed(appr_b))),
                                    color, label))
            continue
        # wrap around the top (default) or bottom, using the pre-planned lane
        # height and per-leg offsets that keep the fan crossing-minimal
        lane_y, off_a, off_b = wrap_plan[id(it)]
        pts = _route_around(pa_pin, box_a, pb_pin, box_b, lane_y, off_a, off_b,
                            part_clear[id(box_a)], part_clear[id(box_b)])
        wires.append(RoutedWire(_clean(pts), color, label))

    # 4. cards in a far-right column
    cards: list[PlacedCard] = []
    right_edge = max((p.box.right for p in placed.values()), default=LEFT)
    cx = right_edge + CHANNEL * 0.55
    cy = TOP
    for ann in diagram.annotations:
        rows = ann.rows
        n = len(rows)
        lines = wrap_card_text(ann.text) if ann.text else []
        # baseline of the last drawn line; +18 below matches the header gap above
        if lines:
            last = FIRST_ROW + n * ROW_H + (len(lines) - 1) * TEXT_LH
        else:
            last = FIRST_ROW + (n - 1) * ROW_H
        h = last + 18
        cards.append(PlacedCard(Box(cx, cy, CARD_W, h), ann.card, rows, ann.text))
        cy += h + CARD_GAP

    # 5. bounds
    xs = [p.box.x for p in placed.values()] + [p.box.right for p in placed.values()]
    ys = [p.box.y for p in placed.values()] + [p.box.bottom for p in placed.values()]
    for wr in wires:
        for (px, py) in wr.points:
            xs.append(px)
            ys.append(py)
    for c in cards:
        xs += [c.box.x, c.box.right]
        ys += [c.box.y, c.box.bottom]
    width = (max(xs) if xs else 400) + LEFT
    # leave a clear gap below the workspace, then room for the legend band
    height = (max(ys) if ys else 300) + 56 + 52

    return Geometry(
        parts=list(placed.values()),
        wires=wires,
        cards=cards,
        legend=list(legend.items()),
        width=width,
        height=height,
        title=diagram.title,
        subtitle=diagram.subtitle,
    )


def _owner_box(placed: dict[str, PlacedPart], pin: PlacedPin) -> Box:
    for p in placed.values():
        if pin in p.pins:
            return p.box
    raise KeyError("pin has no owner")


def _needs_wrap(pin: PlacedPin, own: Box, other: Box) -> bool:
    """True if the pin can't meet the horizontal channel head-on, so the wire
    must wrap over a lane instead. That happens when a side pin points away from
    the other part, or when the pin sits on a top/bottom edge (e.g. a rotated
    board) — a horizontal channel can't receive a vertical pin cleanly."""
    other_is_right = other.cx >= own.cx
    if pin.side == "right":
        return not other_is_right
    if pin.side == "left":
        return other_is_right
    return True  # top/bottom: no head-on approach to a horizontal channel


def _wrap_leg_x(pin: PlacedPin, box: Box, clear: float, off: float = 0.0) -> float:
    """The x at which a wrap wire's vertical leg leaves this endpoint — the pin's
    own x for a top/bottom pin, or just outside the edge (pushed out by ``off``)
    for a side pin. Used to measure each wrap's horizontal span for lane nesting
    and to place the actual leg."""
    if pin.side == "left":
        return box.x - clear - off
    if pin.side == "right":
        return box.right + clear + off
    return pin.x


def _assign_lanes(spans: list[tuple[float, float]]) -> list[int]:
    """Pack wrap wires onto lanes so they cross as little as possible. Each span
    is the [lo, hi] x-range a wrap occupies above/below the parts. Narrower spans
    take inner lanes (index 0 = closest to the parts); a wider span that contains
    or interleaves with one already placed is bumped to an outer lane, so it
    rides over the wires it spans instead of cutting through them. Disjoint spans
    share a lane. This is the crossing-minimal nesting for same-side legs."""
    order = sorted(range(len(spans)), key=lambda i: spans[i][1] - spans[i][0])
    lanes: list[list[tuple[float, float]]] = []
    lane_of = [0] * len(spans)
    for i in order:
        lo, hi = spans[i]
        chosen = None
        for li, occ in enumerate(lanes):
            if all(hi <= o0 or lo >= o1 for (o0, o1) in occ):  # disjoint -> share
                chosen = li
                break
        if chosen is None:
            chosen = len(lanes)
            lanes.append([])
        lanes[chosen].append((lo, hi))
        lane_of[i] = chosen
    return lane_of


def _wrap_side(pa: PlacedPin, pb: PlacedPin, route: str | None) -> str:
    """Which side a wrap detours around. A top-edge pin can only escape upward
    and a bottom-edge pin downward, so an endpoint on a horizontal edge fixes
    the side (else its leg would drop straight through its own board). When
    neither end constrains it, honour the author's ``route`` hint, default top."""
    sides = {pa.side, pb.side}
    if "top" in sides and "bottom" not in sides:
        return "top"
    if "bottom" in sides and "top" not in sides:
        return "bottom"
    return route or "top"


def _plan_wraps(items: list, boxes: list, part_clear: dict, parts_top: float,
                parts_bottom: float) -> dict:
    """Plan every wrap wire's geometry so they cross as little as possible.

    Three decisions per wrap, each a small routing sub-problem:

    * outward offset of each side-pin leg — ranked by the pin's position along
      its edge (top-most pin turns out closest for a top wrap), so a fan of
      wires leaving one edge never crosses itself;
    * lane height — nested by horizontal span (see _assign_lanes), so a wide
      wrap rides outside the narrower ones it spans; and
    * lane base — only as far out as needed to clear the boards actually under
      the wrap's span, so a wire leaving a low board's top edge doesn't climb
      all the way to a taller board's top before turning.

    Returns ``{id(item): (lane_y, off_a, off_b)}``."""
    wraps = [it for it in items if it[7] == "wrap"]
    # effective detour side per wrap (top-edge pins force top, etc.)
    side_of = {id(it): _wrap_side(it[0], it[1], it[4]) for it in wraps}

    # 1. offsets: rank each side-pin exit along its (box, side, direction) edge
    groups: dict = {}
    for it in wraps:
        top = side_of[id(it)] == "top"
        for end in (0, 1):
            pin, box = it[end], it[5 + end]
            if pin.side in ("left", "right"):
                groups.setdefault((id(box), pin.side, top), []).append((pin.y, id(it), end))
    offs: dict = {}
    for (_, _, top), members in groups.items():
        # top wrap climbs upward: the highest pin (smallest y) turns out first
        members.sort(key=lambda m: m[0], reverse=not top)
        for rank, (_, iid, end) in enumerate(members):
            offs[(iid, end)] = rank * WRAP_GAP

    def off_of(it, end):
        return offs.get((id(it), end), 0.0)

    # 2. lanes: nest by the span between the two (now offset) legs, per side
    plan: dict = {}
    for top in (True, False):
        side_items = [it for it in wraps
                      if (side_of[id(it)] == "top") == top]
        spans = []
        for it in side_items:
            xa = _wrap_leg_x(it[0], it[5], part_clear[id(it[5])], off_of(it, 0))
            xb = _wrap_leg_x(it[1], it[6], part_clear[id(it[6])], off_of(it, 1))
            spans.append((min(xa, xb), max(xa, xb)))
        for it, lane, (lo, hi) in zip(side_items, _assign_lanes(spans), spans):
            # clear only the boards beneath this wrap's horizontal span; a span
            # that runs clear of a tall board needn't rise above it. Containment
            # keeps this consistent with nesting: a wider span covers at least
            # the same boards, so it never sits lower than one it contains.
            under = [b for b in boxes if b.x < hi and b.right > lo]
            if top:
                base = (min(b.y for b in under) if under else parts_top) - 16
                lane_y = base - lane * LANE_GAP
            else:
                base = (max(b.bottom for b in under) if under else parts_bottom) + 16
                lane_y = base + lane * LANE_GAP
            plan[id(it)] = (lane_y, off_of(it, 0), off_of(it, 1))
    return plan


def _approach(pin: PlacedPin, own: Box, other: Box,
              track_x: float, lane_y: float) -> list[tuple[float, float]]:
    dir_right = other.cx >= own.cx  # channel is to the right of own part
    facing = "right" if dir_right else "left"
    pts: list[tuple[float, float]] = [(pin.x, pin.y)]
    if pin.side == facing:
        ex = own.right + STUB if dir_right else own.x - STUB
        pts.append((ex, pin.y))
        pts.append((track_x, pin.y))
        return pts
    # route around via the lane above the parts
    if pin.side == "left":
        sx = own.x - STUB
        pts.append((sx, pin.y))
        pts.append((sx, lane_y))
    elif pin.side == "right":
        sx = own.right + STUB
        pts.append((sx, pin.y))
        pts.append((sx, lane_y))
    elif pin.side == "top":
        pts.append((pin.x, lane_y))
    else:  # bottom
        pts.append((pin.x, own.bottom + STUB))
        pts.append((pin.x, lane_y))
    pts.append((track_x, lane_y))
    return pts


def _drop_to_lane(pin: PlacedPin, box: Box, lane_y: float, off: float,
                  clear: float) -> list[tuple[float, float]]:
    """Exit a pin on its own edge and run to a horizontal lane above/below the
    parts. ``clear`` is the base outward clearance (large enough to clear pin
    numbers, small to hug an edge with nothing protruding); ``off`` pushes the
    vertical run further out so parallel wraps on the same edge never share x."""
    if pin.side == "left":
        x = box.x - clear - off
        return [(pin.x, pin.y), (x, pin.y), (x, lane_y)]
    if pin.side == "right":
        x = box.right + clear + off
        return [(pin.x, pin.y), (x, pin.y), (x, lane_y)]
    # top / bottom pins drop straight to the lane
    return [(pin.x, pin.y), (pin.x, lane_y)]


def _route_around(pa: PlacedPin, box_a: Box, pb: PlacedPin, box_b: Box,
                  lane_y: float, off_a: float, off_b: float, clear_a: float,
                  clear_b: float) -> list[tuple[float, float]]:
    """Route a wire around the top/bottom: each end drops to a shared lane near
    its own part edge (its leg pushed out by its own ``off``), then they meet
    along the lane (kept out of the channel)."""
    a = _drop_to_lane(pa, box_a, lane_y, off_a, clear_a)
    b = _drop_to_lane(pb, box_b, lane_y, off_b, clear_b)
    return a + list(reversed(b))


def _side_jumper(a: PlacedPin, b: PlacedPin, box: Box,
                 depth: float) -> list[tuple[float, float]]:
    """A short orthogonal jumper between two pins on the same edge of one part,
    bulging ``depth`` px outward from that edge."""
    if a.side == "left":
        x = box.x - depth
        return [(a.x, a.y), (x, a.y), (x, b.y), (b.x, b.y)]
    if a.side == "right":
        x = box.right + depth
        return [(a.x, a.y), (x, a.y), (x, b.y), (b.x, b.y)]
    if a.side == "top":
        y = box.y - depth
        return [(a.x, a.y), (a.x, y), (b.x, y), (b.x, b.y)]
    y = box.bottom + depth
    return [(a.x, a.y), (a.x, y), (b.x, y), (b.x, b.y)]


def _nearest_pair(ca: list[PlacedPin], cb: list[PlacedPin]):
    best = None
    best_d = None
    for a in ca:
        for b in cb:
            d = (a.x - b.x) ** 2 + (a.y - b.y) ** 2
            if best_d is None or d < best_d:
                best_d = d
                best = (a, b)
    return best


def _clean(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for p in points:
        if not out or (abs(p[0] - out[-1][0]) > 0.01 or abs(p[1] - out[-1][1]) > 0.01):
            out.append(p)
    return out
