from pathlib import Path

import yaml

from wiregen.layout import build
from wiregen.library import Library
from wiregen.model import Diagram
from wiregen.render import render
from wiregen.table import build_rows
from wiregen.theme import THEMES, get_theme
from wiregen.validate import validate

EXAMPLE = Path(__file__).parent.parent / "examples" / "pcm1808_to_dspi.yaml"


def _load():
    lib = Library.load()
    diagram = Diagram.model_validate(yaml.safe_load(EXAMPLE.read_text()))
    return diagram, lib


def test_library_loads():
    lib = Library.load()
    assert "pico2" in lib.ids()
    assert "pcm1808" in lib.ids()          # bare IC
    assert "pcm1808_board" in lib.ids()    # purple breakout


def test_board_has_purple_substrate():
    lib = Library.load()
    board = lib.get("pcm1808_board")
    assert board.color and board.color.startswith("#")
    assert len(board.decorations) > 0


def test_example_validates_clean():
    diagram, lib = _load()
    issues = validate(diagram, lib)
    assert [i for i in issues if i.severity == "error"] == []


def test_example_renders_svg():
    diagram, lib = _load()
    svg = render(build(diagram, lib))
    assert svg.startswith("<svg")
    assert svg.rstrip().endswith("</svg>")
    assert "PCM1808" in svg


def test_bus_expands_to_four_wires():
    diagram, lib = _load()
    geo = build(diagram, lib)
    # 4 i2s + 5v + 3v3 + gnd + 3 strapping jumpers = 10 wires
    assert len(geo.wires) == 10


def test_connection_table_has_rows():
    diagram, lib = _load()
    rows = build_rows(diagram, lib)
    pairs = {(a_pin, b_pin) for _, a_pin, _, b_pin, _ in rows}
    assert ("GP14", "BCK") in pairs
    assert ("GP4", "OUT") in pairs


def test_themes_render_and_adapt():
    diagram, lib = _load()
    for name, theme in THEMES.items():
        svg = render(build(diagram, lib, theme), theme)
        assert f'fill="{theme.bg}"' in svg
    # default (dark) has a light ground wire; light theme a dark one
    assert get_theme("neutral-dark").nets["gnd"] != get_theme("light").nets["gnd"]


def test_unknown_theme_raises():
    try:
        get_theme("does-not-exist")
    except KeyError as e:
        assert "does-not-exist" in str(e)
    else:
        raise AssertionError("expected KeyError")


def test_rotate_normalises_and_validates():
    # negative / over-360 normalise into [0, 360)
    d = Diagram.model_validate({
        "parts": {"adc": {"part": "pcm1808_board", "region": "left",
                          "rotate": -90}}})
    assert d.parts["adc"].rotate == 270
    # non-quarter turns are rejected
    try:
        Diagram.model_validate({
            "parts": {"adc": {"part": "pcm1808_board", "rotate": 45}}})
    except Exception as e:
        assert "multiple of 90" in str(e)
    else:
        raise AssertionError("expected a validation error for rotate=45")


def test_rotate_moves_pins_and_swaps_footprint():
    lib = Library.load()

    def sides_and_box(rot):
        d = Diagram.model_validate({
            "parts": {"adc": {"part": "pcm1808_board", "region": "left",
                              "rotate": rot}}})
        p = build(d, lib).parts[0]
        sides = {pin.side for pin in p.pins}
        return sides, (round(p.box.w), round(p.box.h)), p.pins

    s0, box0, _ = sides_and_box(0)
    s90, box90, pins90 = sides_and_box(90)
    assert "top" in s0 and "left" in s0 and "right" in s0
    # 90deg clockwise: left->top, top->right, right->bottom
    assert s90 == {"top", "right", "bottom"}
    # a quarter turn puts the old vertical edge on top: its height becomes the
    # rotated width, which then grows because side pins get a wider pitch there
    # footprint width/height swap for a quarter turn
    assert box90 == (box0[1], box0[0])
    # the now-top edge keeps the standard pin pitch (no rotation special case)
    from wiregen.layout import PIN_PITCH
    top_xs = sorted(pin.x for pin in pins90 if pin.side == "top")
    assert round(top_xs[1] - top_xs[0]) == PIN_PITCH
    # every pin still sits exactly on the (rotated) body boundary
    b = build(Diagram.model_validate(
        {"parts": {"adc": {"part": "pcm1808_board", "rotate": 90}}}), lib).parts[0]
    for pin in b.pins:
        on = (abs(pin.x - b.box.x) < 0.5 or abs(pin.x - b.box.right) < 0.5
              or abs(pin.y - b.box.y) < 0.5 or abs(pin.y - b.box.bottom) < 0.5)
        assert on, f"pin {pin.text} off the boundary"


def test_rotation_preserves_pin_numbers_and_renders():
    # a turned dual-header board keeps its numbered pads (now on top/bottom)
    lib = Library.load()
    d = Diagram.model_validate(
        {"parts": {"pico": {"part": "pico2", "region": "left", "rotate": 90}}})
    geo = build(d, lib)
    numbers = {pin.number for pin in geo.parts[0].pins if pin.number}
    assert {"1", "20", "40"} <= numbers
    svg = render(geo)
    assert svg.startswith("<svg") and ">40<" in svg  # pin number 40 is drawn


def _seg_int(p, q, r, s):
    """Proper interior intersection of segments pq and rs, else None."""
    def cr(ax, ay, bx, by):
        return ax * by - ay * bx
    rx, ry, sx, sy = q[0]-p[0], q[1]-p[1], s[0]-r[0], s[1]-r[1]
    denom = cr(rx, ry, sx, sy)
    if abs(denom) < 1e-9:
        return None
    t = cr(r[0]-p[0], r[1]-p[1], sx, sy) / denom
    u = cr(r[0]-p[0], r[1]-p[1], rx, ry) / denom
    return (p[0]+t*rx, p[1]+t*ry) if 1e-6 < t < 1-1e-6 and 1e-6 < u < 1-1e-6 else None


def _pair_crossings(geo):
    """For each unordered wire pair, how many times they cross."""
    def segs(w):
        return list(zip(w.points, w.points[1:]))
    out = []
    ws = geo.wires
    for i in range(len(ws)):
        for j in range(i+1, len(ws)):
            n = sum(1 for a, b in segs(ws[i]) for c, e in segs(ws[j])
                    if _seg_int(a, b, c, e))
            if n:
                out.append(n)
    return out


def test_routing_is_crossing_minimal():
    # The router must never make two wires cross more than once: a pair that
    # crosses twice is wasted routing that could be straightened out. Every
    # remaining crossing should be a single, intrinsic interleaving. This holds
    # even for the awkward rotated layout, where a naive router tangled badly.
    lib = Library.load()
    for name in ("pcm1808_to_dspi.yaml", "adc_rotated_to_pico.yaml",
                 "rotation_demo.yaml"):
        path = Path(__file__).parent.parent / "examples" / name
        d = Diagram.model_validate(yaml.safe_load(path.read_text()))
        counts = _pair_crossings(build(d, lib))
        assert all(c == 1 for c in counts), \
            f"{name}: wire pairs crossing >1 time: {[c for c in counts if c > 1]}"


def _wire_length(geo):
    total = 0.0
    for w in geo.wires:
        for a, b in zip(w.points, w.points[1:]):
            total += ((a[0]-b[0])**2 + (a[1]-b[1])**2) ** 0.5
    return total


def test_wraps_dont_overshoot_their_obstacle():
    # A wire leaving a low board's top edge should turn just above that board,
    # not climb all the way to a taller board's top. Regression guard: with the
    # global-lane bug the rotated example's wiring ran ~7800px; clearing only the
    # boards beneath each wrap brings it well under that.
    lib = Library.load()
    path = Path(__file__).parent.parent / "examples" / "adc_rotated_to_pico.yaml"
    d = Diagram.model_validate(yaml.safe_load(path.read_text()))
    geo = build(d, lib)
    assert _wire_length(geo) < 6800, "rotated wiring is overshooting obstacles"
    # the ADC's top-edge pins (y == box.bottom side after the turn) sit at the
    # board's top; no wire should rise more than a few lanes above it before
    # turning. Find the ADC and check the first run off each of its top pins.
    adc = next(p for p in geo.parts if p.iid == "adc")
    top_y = min(pin.y for pin in adc.pins)            # the top edge pins' y
    top_xs = {round(pin.x) for pin in adc.pins if abs(pin.y - top_y) < 0.5}
    for w in geo.wires:
        end = w.points[-1] if round(w.points[-1][1]) == round(top_y) else w.points[0]
        if round(end[0]) in top_xs and round(end[1]) == round(top_y):
            nbr = w.points[-2] if end is w.points[-1] else w.points[1]
            assert abs(nbr[1] - end[1]) < 130, "overlong run leaving the ADC top edge"


def test_rca_stereo_loads_and_colours_jacks():
    # the blue RCA breakout: its two jack-face holes carry an explicit colour
    lib = Library.load()
    rca = lib.get("rca_stereo")
    faces = [d for d in rca.decorations if d.type == "hole" and d.color]
    assert len(faces) == 2
    svg = render(build(Diagram.model_validate(
        {"parts": {"j": {"part": "rca_stereo"}}}), lib))
    assert any(f.color in svg for f in faces)   # the jack colour reaches the SVG


def test_unknown_pin_is_error():
    lib = Library.load()
    d = Diagram.model_validate({
        "parts": {"pico": {"part": "pico2", "region": "left"},
                  "adc": {"part": "pcm1808", "region": "right"}},
        "connections": [{"from": "pico.GP999", "to": "adc.BCK"}],
    })
    issues = validate(d, lib)
    assert any(i.severity == "error" and "GP999" in i.message for i in issues)
