"""Parametric standard cap screws, generated from a SolidWorks configuration name.

Why: a configured fastener part (one ``SHCS*`` or ``BHCS*`` file, a
few hundred size configurations) stores Parasolid geometry for one size only
(ISSUES.md #1).  The configuration name carries the spec, e.g.
``M6x1.0|10mm|18-8 SS|FULL`` or ``M6×1|6mm|18-8|FULL``, so the screw can be
generated from standard dimension tables instead.

Families
    SHCS  socket head cap screw, ISO 4762 / DIN 912   (file stem ``SHCS*``)
    BHCS  button head cap screw, ISO 7380-1           (file stem ``BHCS*``)

Geometry (millimetres, in the part's own frame, the convention of the stored
bodies in configured vendor fastener files, verified by comparing the
generated stored-configuration screw with the converted body):
    * axis +Y, head bearing face on y = 0, head in 0 <= y <= k, tip at y = -L
    * thread = plain cylinder at nominal diameter d, no helix; for PARTIAL
      thread the shank face is split at the thread start (y = -L + b), so the
      threaded length shows as an edge
    * SHCS head = cylinder dk x k; BHCS head = short cylinder then a toroidal
      dome with a flat top (profile calibrated on the stored M3 body)
    * hexagon socket, width across flats s, flat bottom, depth 0.75 k (as the
      stored bodies), two flats normal to Z
    * no chamfers, fillets or under-head radius (the stored bodies have none)

Naming: ``<FAM> M<d>x<pitch>x<length>`` plus `` PT`` for a partial thread,
e.g. ``SHCS M6x1.0x10``, ``BHCS M6x1.0x6``, ``SHCS M4x0.7x40 PT``.
"""
from __future__ import annotations

import fnmatch
import math
import re
from dataclasses import dataclass

from OCP.gp import gp_Pnt, gp_Dir, gp_Ax1, gp_Ax2
from OCP.GC import GC_MakeArcOfCircle
from OCP.BRepBuilderAPI import (BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeWire,
                                BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon)
from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakeRevol, BRepPrimAPI_MakePrism
from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse, BRepAlgoAPI_Cut
from OCP.gp import gp_Vec
from OCP.TopoDS import TopoDS_Shape

# ISO 4762 (max head sizes, nominal key size); b = 2d+12 (l <= 125), 2d+24 (<= 200), 2d+57
#        d:   (dk,   k,    s)
SHCS = {1.6: (3.0, 1.6, 1.5),
        2.0: (3.8, 2.0, 1.5),
        2.5: (4.5, 2.5, 2.0),
        3.0: (5.5, 3.0, 2.5),
        4.0: (7.0, 4.0, 3.0),
        5.0: (8.5, 5.0, 4.0),
        6.0: (10.0, 6.0, 5.0),
        8.0: (13.0, 8.0, 6.0),
        10.0: (16.0, 10.0, 8.0),
        12.0: (18.0, 12.0, 10.0),
        14.0: (21.0, 14.0, 12.0),
        16.0: (24.0, 16.0, 14.0),
        20.0: (30.0, 20.0, 17.0),
        24.0: (36.0, 24.0, 19.0)}

# ISO 7380-1 (max head sizes, nominal key size) covers M3..M16.  M2 / M2.5
# are not in ISO 7380-1; those rows are common manufacturer values.
BHCS = {2.0: (3.5, 1.3, 1.3),       # not ISO 7380-1
        2.5: (4.7, 1.5, 1.5),       # not ISO 7380-1
        3.0: (5.7, 1.65, 2.0),
        4.0: (7.6, 2.2, 2.5),
        5.0: (9.5, 2.75, 3.0),
        6.0: (10.5, 3.3, 4.0),
        8.0: (14.0, 4.4, 5.0),
        10.0: (17.5, 5.5, 6.0),
        12.0: (21.0, 6.6, 8.0),
        16.0: (28.0, 8.8, 10.0)}

TABLES = {"SHCS": SHCS, "BHCS": BHCS}
STANDARD = {"SHCS": "ISO 4762", "BHCS": "ISO 7380-1"}
# file stems (upper case, Pack-and-Go suffix stripped by the caller or matched by '*')
FAMILY_GLOBS = {"SHCS": ("SHCS", "SHCS_*"), "BHCS": ("BHCS", "BHCS_*")}

SOCKET_DEPTH = 0.75       # x k, as in the stored bodies (ISO gives only t min)
# BHCS dome, calibrated on the stored M3x0.5 body: edge cylinder height c,
# torus tube radius rho centred on the bearing plane, flat top at y = k.
DOME_C = 0.1717           # x k
DOME_RHO = 1.0097         # x k

_CFG = re.compile(r"^\s*M(\d+(?:\.\d+)?)\s*[x×X]\s*(\d+(?:\.\d+)?)\s*\|\s*(\d+(?:\.\d+)?)\s*mm\s*\|"
                  r"[^|]*\|\s*(FULL|PARTIAL)\s*$", re.I)


@dataclass(frozen=True)
class Screw:
    family: str        # SHCS / BHCS
    d: float           # nominal diameter, mm
    pitch: float
    length: float      # nominal length (under head), mm
    full: bool         # fully threaded
    b: float | None = None   # thread length from the part's properties, if any

    @property
    def dims(self):
        return TABLES[self.family][self.d]

    @property
    def thread_length(self) -> float:
        if self.full:
            return self.length
        if self.b:
            return min(self.b, self.length)
        b = 2 * self.d + (12 if self.length <= 125 else 24 if self.length <= 200 else 57)
        return min(b, self.length)

    @property
    def name(self) -> str:
        return (f"{self.family} M{_num(self.d)}x{_num(self.pitch, 1)}x{_num(self.length)}"
                + ("" if self.full else " PT"))


def _num(x: float, min_dec: int = 0) -> str:
    s = f"{x:.3f}".rstrip("0").rstrip(".")
    if min_dec and "." not in s:
        s += ".0"
    return s


def family(stem: str) -> str | None:
    """Parametric family of a component file stem, or None."""
    s = stem.upper()
    for fam, globs in FAMILY_GLOBS.items():
        if any(fnmatch.fnmatchcase(s, g) for g in globs):
            return fam
    return None


def parse(fam: str, config: str) -> Screw | None:
    """Screw spec from a configuration name, or None when it does not parse or
    the size is not in the table."""
    m = _CFG.match(config or "")
    if not m or fam not in TABLES:
        return None
    d, p, l = float(m.group(1)), float(m.group(2)), float(m.group(3))
    if d not in TABLES[fam] or l <= 0 or p <= 0:
        return None
    return Screw(fam, d, p, l, m.group(4).upper() == "FULL")


def _cyl(r, y0, h):
    return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, y0, 0), gp_Dir(0, 1, 0)), r, h).Shape()


def _bhcs_head(dk, k):
    """Revolve the half profile (x = radius, y = axial) about +Y."""
    c, rho = DOME_C * k, DOME_RHO * k
    rc = dk / 2 - math.sqrt(rho * rho - c * c)
    rtop = rc + math.sqrt(rho * rho - k * k)
    # a point on the arc between the edge and the top, for the 3-point arc
    ym = 0.5 * (c + k)
    xm = rc + math.sqrt(rho * rho - ym * ym)
    p = [gp_Pnt(0, 0, 0), gp_Pnt(dk / 2, 0, 0), gp_Pnt(dk / 2, c, 0), gp_Pnt(rtop, k, 0), gp_Pnt(0, k, 0)]
    arc = GC_MakeArcOfCircle(p[2], gp_Pnt(xm, ym, 0), p[3]).Value()
    w = BRepBuilderAPI_MakeWire()
    w.Add(BRepBuilderAPI_MakeEdge(p[0], p[1]).Edge())
    w.Add(BRepBuilderAPI_MakeEdge(p[1], p[2]).Edge())
    w.Add(BRepBuilderAPI_MakeEdge(arc).Edge())
    w.Add(BRepBuilderAPI_MakeEdge(p[3], p[4]).Edge())
    w.Add(BRepBuilderAPI_MakeEdge(p[4], p[0]).Edge())
    face = BRepBuilderAPI_MakeFace(w.Wire(), True).Face()
    return BRepPrimAPI_MakeRevol(face, gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 1, 0)), 2 * math.pi).Shape()


def _hex_socket(s, k, depth):
    """Hexagonal prism, flats s apart (two flats normal to Z), from y = k - depth
    up to above the head top."""
    r = s / math.sqrt(3)
    y0 = k - depth
    poly = BRepBuilderAPI_MakePolygon()
    for i in range(6):
        a = math.radians(60 * i)
        poly.Add(gp_Pnt(r * math.cos(a), y0, r * math.sin(a)))
    poly.Close()
    face = BRepBuilderAPI_MakeFace(poly.Wire(), True).Face()
    return BRepPrimAPI_MakePrism(face, gp_Vec(0, depth + 1.0, 0)).Shape()


def make(screw: Screw) -> TopoDS_Shape:
    """Solid of the screw in the part frame described in the module docstring."""
    dk, k, s = screw.dims
    r, L = screw.d / 2, screw.length
    head = _cyl(dk / 2, 0.0, k) if screw.family == "SHCS" else _bhcs_head(dk, k)
    b = screw.thread_length
    if b < L:   # plain shank + threaded part: the shared circle marks the thread start
        shank = BRepAlgoAPI_Fuse(_cyl(r, -L, b), _cyl(r, -L + b, L - b)).Shape()
    else:
        shank = _cyl(r, -L, L)
    body = BRepAlgoAPI_Fuse(head, shank).Shape()
    return BRepAlgoAPI_Cut(body, _hex_socket(s, k, SOCKET_DEPTH * k)).Shape()


_TL = re.compile(r'name="ThreadLength"[^>]*>\s*<vt:lpstr>\s*([\d.]+)\s*<')


def _thread_lengths(path) -> dict:
    """{configuration name: ThreadLength custom property (mm)} of a part file.
    Vendor fastener parts carry the catalogue thread length there; it follows
    ISO 4762 b up to l = 125 and stays at 2d+12 above (ISO: 2d+24)."""
    from sldprt2xt.container import open_container
    from sldload import read_configs
    c = open_container(path)
    names = read_configs(c).names
    out = {}
    for blob in c.blobs:
        m = re.search(r"Config-(\d+)-Properties", blob.name)
        if m and m.group(1) in names:
            t = _TL.search(blob.data.decode("utf-8", "replace"))
            if t:
                out[names[m.group(1)]] = float(t.group(1))
    return out


class Generator:
    """Screw specs and solids for assembly instances, cached."""

    def __init__(self):
        self._tl = {}       # part path -> {config: thread length}
        self._shapes = {}   # Screw -> shape

    def screw(self, stem: str, config: str, part_path=None) -> Screw | None:
        """Spec for a component (file stem, configuration name), or None when the
        stem is no known family or the configuration does not parse."""
        fam = family(stem)
        sc = parse(fam, config) if fam else None
        if sc is None or sc.full or part_path is None:
            return sc
        key = str(part_path)
        if key not in self._tl:
            try:
                self._tl[key] = _thread_lengths(part_path)
            except Exception:
                self._tl[key] = {}
        b = self._tl[key].get(config)
        return Screw(sc.family, sc.d, sc.pitch, sc.length, sc.full, b) if b else sc

    def shape(self, sc: Screw) -> TopoDS_Shape:
        if sc not in self._shapes:
            self._shapes[sc] = make(sc)
        return self._shapes[sc]
