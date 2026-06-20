"""Declarative data model for wiring diagrams.

Two kinds of documents live here:

* Part definitions (``PartDef``) describe a reusable component; one YAML file
  per part under ``wiregen/parts/``. A part is a body plus an ordered list of
  pins, each placed on a physical side. No pixel coordinates ever appear.
* Diagram documents (``Diagram``) describe one picture: which parts are placed,
  how they are wired, and any annotation cards. This is the single surface a
  user (or an LLM) authors.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

Side = Literal["left", "right", "top", "bottom"]
Region = Literal["left", "center", "right"]
Direction = Literal["in", "out", "bidir", "power", "passive"]


class PinDef(BaseModel):
    """One pin on a part. ``side`` is physical (matches the real package);
    layout derives the exact position from side + order in the pin list."""

    model_config = ConfigDict(extra="forbid")

    name: str
    side: Side = "right"
    signal: Optional[str] = None  # bus/standard family: i2s, spdif, i2c, power, analog...
    role: Optional[str] = None    # role within the family: bck, lrck, sd, mck, sda, vcc...
    dir: Direction = "passive"
    net: Optional[str] = None     # electrical net tag for grouping/colour: gnd, 3v3, 5v
    label: Optional[str] = None   # display text; defaults to name
    number: Optional[str] = None  # physical pin number / silk designator

    @property
    def text(self) -> str:
        return self.label or self.name


class Decoration(BaseModel):
    """A simplified on-board component drawn for visual likeness (electrolytic
    cap, SMD resistor, the IC, etc.). ``at`` is a normalised [x, y] position
    inside the body (0..1), so it scales with the board. Purely cosmetic."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["cap", "resistor", "ic", "dot", "hole", "button", "logo"]
    at: tuple[float, float]
    r: Optional[float] = None      # cap/dot radius (px)
    w: Optional[float] = None      # resistor/ic width (px)
    h: Optional[float] = None      # resistor/ic height (px)
    label: Optional[str] = None    # e.g. the IC part number
    color: Optional[str] = None    # override the default fill (e.g. RCA jack face)
    rot: float = 0.0               # rotation in degrees about the centre


class PartDef(BaseModel):
    """A reusable component definition loaded from ``wiregen/parts/*.yaml``."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    package: Literal["board", "module", "ic", "header", "component"] = "module"
    style: Optional[str] = None        # render hint: "pcb", "chip", "card"
    color: Optional[str] = None        # board substrate colour (e.g. purple PCB)
    connector: Optional[str] = None    # top-edge connector hint, e.g. "usb"
    description: Optional[str] = None
    pins: list[PinDef]
    decorations: list[Decoration] = Field(default_factory=list)
    width: Optional[float] = None       # optional body-size override (px)
    height: Optional[float] = None


# --- diagram document -------------------------------------------------------


class PartInstance(BaseModel):
    """A placed instance of a part within a diagram."""

    model_config = ConfigDict(extra="forbid")

    part: str                          # PartDef id
    label: Optional[str] = None        # overrides the part's display name
    region: Region = "center"          # coarse layout hint; engine owns coordinates
    order: Optional[int] = None        # vertical order within the region
    flip: bool = False                 # mirror left<->right so a header faces the channel
    rotate: int = 0                    # turn the part clockwise: 0, 90, 180 or 270 deg
    offset_x: float = 0.0              # manual horizontal nudge in px (+ right)
    offset_y: float = 0.0              # manual vertical nudge in px (+ down)

    @field_validator("rotate")
    @classmethod
    def _quarter_turn(cls, v: int) -> int:
        # accept any signed multiple of 90 and normalise to [0, 360); pins,
        # labels and wiring all follow, so only orthogonal turns are allowed.
        v = int(v) % 360
        if v % 90 != 0:
            raise ValueError("rotate must be a multiple of 90 (0, 90, 180, 270)")
        return v


class Connection(BaseModel):
    """Either an explicit pin-to-pin wire (``from``/``to``) or a bus shorthand
    (``bus`` + ``map``) that expands into several consistently styled wires."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # explicit form
    from_: Optional[str] = Field(default=None, alias="from")
    to: Optional[str] = None
    # bus form
    bus: Optional[str] = None
    map: Optional[dict[str, str]] = None
    # styling / labelling (optional, applies to either form)
    label: Optional[str] = None
    color: Optional[str] = None
    signal: Optional[str] = None
    route: Optional[Literal["top", "bottom"]] = None  # detour side for wraps


class Annotation(BaseModel):
    """A floating information card; key/value ``rows`` and/or free ``text``."""

    model_config = ConfigDict(extra="forbid")

    card: str                          # title
    anchor: Optional[str] = None       # instance id to sit beside (advisory)
    region: Optional[Region] = None    # explicit placement column
    rows: dict[str, str] = Field(default_factory=dict)
    text: Optional[str] = None


class Diagram(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = None
    subtitle: Optional[str] = None
    parts: dict[str, PartInstance]
    connections: list[Connection] = Field(default_factory=list)
    annotations: list[Annotation] = Field(default_factory=list)
    theme: Optional[str] = None
    grid: bool = False                 # draw a faint background grid
