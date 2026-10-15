"""SolidWorks .SLDPRT -> STEP, fully offline, open-source stack.

Pipeline:  sldprt2xt (container + Parasolid node decode, full 13006 schema)
           -> this module rebuilds the B-rep in Open CASCADE (OCP bindings)
           -> STEP AP214 writer.

Parasolid conventions used (Parasolid XT Format Reference):
  * lengths are metres; we scale to millimetres (SCALE)
  * curve->sense '-' : edge direction (start->end) opposes the curve parameter
  * fin->sense  '-' : fin direction opposes its edge
  * face normal == natural surface normal iff face.sense == surface.sense
  * shell.face chain = back faces (normal points OUT of the shell's region),
    shell.front_face chain = front faces (normal points INTO the region)
  * edge.halfedge is the positive fin: its vertex is the edge's END vertex,
    and edge.halfedge.other.vertex is the START vertex
  * loop fins are ordered nose-to-tail via fin.forward, face on the left
"""
from __future__ import annotations

import json
import math
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from OCP.gp import (gp_Pnt, gp_Dir, gp_Vec, gp_Ax1, gp_Ax2, gp_Ax3, gp_Pnt2d,
                    gp_Trsf2d, gp_GTrsf2d, gp_Mat2d, gp_XY, gp_Vec2d, gp_Dir2d, gp_Ax2d)
from OCP.Geom import (Geom_Plane, Geom_CylindricalSurface, Geom_ConicalSurface,
                      Geom_SphericalSurface, Geom_ToroidalSurface, Geom_BSplineSurface,
                      Geom_SurfaceOfLinearExtrusion, Geom_SurfaceOfRevolution,
                      Geom_OffsetSurface, Geom_Line, Geom_Circle, Geom_Ellipse,
                      Geom_BSplineCurve, Geom_Curve, Geom_Surface,
                      Geom_RectangularTrimmedSurface)
from OCP.Geom2d import Geom2d_BSplineCurve, Geom2d_Curve
from OCP.TColgp import (TColgp_Array1OfPnt, TColgp_Array2OfPnt, TColgp_Array1OfPnt2d,
                        TColgp_HArray1OfPnt)
from OCP.TColStd import TColStd_Array1OfReal, TColStd_Array1OfInteger, TColStd_Array2OfReal
from OCP.GeomAPI import (GeomAPI_Interpolate, GeomAPI_ProjectPointOnCurve, GeomAPI_IntSS,
                         GeomAPI_ProjectPointOnSurf, GeomAPI_PointsToBSplineSurface)
from OCP.GeomAbs import GeomAbs_C2
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeFace
from OCP.BRep import BRep_Builder, BRep_Tool
from OCP.TopoDS import (TopoDS_Vertex, TopoDS_Edge, TopoDS_Wire, TopoDS_Face, TopoDS_Shell,
                        TopoDS_Solid, TopoDS_Compound, TopoDS_Shape, TopoDS)
from OCP.TopAbs import TopAbs_FORWARD, TopAbs_REVERSED, TopAbs_IN, TopAbs_ON
from OCP.ShapeFix import ShapeFix_Face, ShapeFix_Wire, ShapeFix_Shape, ShapeFix_Solid
from OCP.BRepLib import BRepLib
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.GProp import GProp_GProps
from OCP.BRepGProp import BRepGProp
from OCP.Bnd import Bnd_Box
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepClass3d import BRepClass3d_SolidClassifier
from OCP.STEPControl import STEPControl_Writer, STEPControl_AsIs
from OCP.Interface import Interface_Static
from OCP.IFSelect import IFSelect_RetDone
from OCP.TopExp import TopExp_Explorer
from OCP.TopAbs import TopAbs_FACE, TopAbs_EDGE, TopAbs_SOLID, TopAbs_SHELL

SCALE = 1000.0          # Parasolid metres -> STEP millimetres
PREC = 1e-5             # mm; Parasolid linear resolution is 1e-8 m
MAXTOL = 1e-2           # mm; ShapeFix may raise tolerances up to this
NULL_DOUBLE = -31415800000000.0
TWO_PI = 2.0 * math.pi
import os
FIX_MODE = int(os.environ.get("FIX_MODE", "1"))   # 1 = in-place pcurves + shape-wide heal (default); 0/2/3 are dev modes


def _is_null(x) -> bool:
    return x is None or (isinstance(x, float) and abs(x - NULL_DOUBLE) < 1.0)


def _ch(v) -> str:
    if isinstance(v, int):
        return chr(v)
    return str(v)


def _plus(v) -> bool:
    return _ch(v) == "+"


def P(v) -> gp_Pnt:
    return gp_Pnt(v[0] * SCALE, v[1] * SCALE, v[2] * SCALE)


def D(v) -> gp_Dir:
    return gp_Dir(v[0], v[1], v[2])


def _foot(pnt, surf):
    """Nearest point of a surface to pnt, robust for infinite/periodic surfaces."""
    from OCP.ShapeAnalysis import ShapeAnalysis_Surface
    pr = GeomAPI_ProjectPointOnSurf(pnt, surf)
    if pr.NbPoints() > 0:
        return pr.NearestPoint()
    uv = ShapeAnalysis_Surface(surf).ValueOfUV(pnt, PREC)
    return surf.Value(uv.X(), uv.Y())


def _list(values: dict):
    """The single list-valued field of a data node (knots, mults, vertices, hvecs)."""
    for v in values.values():
        if isinstance(v, list):
            return v
    for v in values.values():
        if isinstance(v, (int, float)):
            return [v]
    return []


def _field(values: dict, name: str):
    """A list-valued field by name (scalar promoted to a one-element list)."""
    if name in values:
        v = values[name]
        return v if isinstance(v, list) else [v]
    return _list(values)


class Unsupported(Exception):
    pass


@dataclass
class ParamMap:
    """Affine map from Parasolid (u,v) to OCCT (u,v) for the same surface, plus
    whether the OCCT natural normal is opposite to Parasolid's."""
    a: float = 1.0   # u' = a*u + b*v + e
    b: float = 0.0
    c: float = 0.0   # v' = c*u + d*v + f
    d: float = 1.0
    e: float = 0.0
    f: float = 0.0
    flip: bool = False

    def is_identity(self):
        return (self.a, self.b, self.c, self.d, self.e, self.f) == (1, 0, 0, 1, 0, 0)

    def apply(self, u, v):
        return self.a * u + self.b * v + self.e, self.c * u + self.d * v + self.f


@dataclass
class Report:
    part: str
    bodies: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    seconds: float = 0.0
    geom_checks: dict = field(default_factory=dict)   # kind -> {"n": .., "bad": .., "max": ..}

    def check(self, kind: str, dev: float, limit: float = 1e-2):
        c = self.geom_checks.setdefault(kind, {"n": 0, "bad": 0, "max": 0.0})
        c["n"] += 1
        c["max"] = max(c["max"], dev)
        if dev > limit:
            c["bad"] += 1


class Converter:
    def __init__(self, nodes, report: Report | None = None):
        self.by = {n.index: n for n in nodes}
        self.nodes = nodes
        self.rep = report or Report("?")
        self._curves: dict[int, Geom_Curve] = {}
        self._surfs: dict[int, tuple[Geom_Surface, ParamMap]] = {}
        self._verts: dict[int, TopoDS_Vertex] = {}
        self._edges: dict[int, TopoDS_Edge] = {}
        self._edge_closed: dict[int, bool] = {}
        self._blend_eval: dict[int, dict] = {}
        self._edge_sense: dict[int, bool] = {}
        self.builder = BRep_Builder()

    # ------------------------------------------------------------------ util
    def faces_of_surface(self, sidx) -> list[int]:
        if not hasattr(self, "_faces_by_surface"):
            m: dict[int, list[int]] = {}
            for n in self.nodes:
                if n.name == "FACE" and n.values.get("surface"):
                    m.setdefault(n.values["surface"], []).append(n.index)
            self._faces_by_surface = m
        return self._faces_by_surface.get(sidx, [])

    def n(self, idx):
        return self.by[idx] if idx else None

    def warn(self, msg):
        if msg not in self.rep.warnings:
            self.rep.warnings.append(msg)

    def chain(self, head, key):
        out, seen = [], set()
        cur = head
        while cur and cur not in seen:
            seen.add(cur)
            out.append(cur)
            cur = self.by[cur].values.get(key, 0)
        return out

    # ------------------------------------------------------------- NURBS data
    def _nurbs_curve_data(self, nc):
        v = nc.values
        deg = v["degree"]; nv = v["n_vertices"]; dim = v["vertex_dim"]
        rational = bool(v.get("rational"))
        periodic = bool(v.get("periodic"))
        verts = _field(self.by[v["bspline_vertices"]].values, "vertices")
        mults = [int(m) for m in _field(self.by[v["knot_mult"]].values, "mult")]
        knots = [float(k) for k in _field(self.by[v["knots"]].values, "knots")]
        return deg, nv, dim, rational, periodic, verts, knots, mults

    def curve_bspline(self, nc, scale=SCALE) -> Geom_BSplineCurve:
        deg, nv, dim, rational, periodic, verts, knots, mults = self._nurbs_curve_data(nc)
        poles = TColgp_Array1OfPnt(1, nv)
        weights = TColStd_Array1OfReal(1, nv)
        for i in range(nv):
            row = verts[i * dim:(i + 1) * dim]
            if rational:
                w = row[-1]
                xyz = [c / w for c in row[:-1]]
            else:
                w = 1.0
                xyz = list(row)
            while len(xyz) < 3:
                xyz.append(0.0)
            poles.SetValue(i + 1, gp_Pnt(xyz[0] * scale, xyz[1] * scale, xyz[2] * scale))
            weights.SetValue(i + 1, w)
        return self._make_bspline(poles, weights, knots, mults, deg, periodic, nv, rational)

    def _make_bspline(self, poles, weights, knots, mults, deg, periodic, nv, rational):
        k = TColStd_Array1OfReal(1, len(knots))
        m = TColStd_Array1OfInteger(1, len(mults))
        for i, (kk, mm) in enumerate(zip(knots, mults)):
            k.SetValue(i + 1, kk); m.SetValue(i + 1, mm)
        total = sum(mults)
        if total == nv + deg + 1:
            c = Geom_BSplineCurve(poles, weights, k, m, deg, False, rational)
            # clamp to the defined range when the ends are not clamped
            if mults[0] < deg + 1 or mults[-1] < deg + 1:
                flat = [kk for kk, mm in zip(knots, mults) for _ in range(mm)]
                u1, u2 = flat[deg], flat[len(flat) - 1 - deg]
                c.Segment(u1, u2)
            return c
        if periodic and total - mults[-1] == nv:
            return Geom_BSplineCurve(poles, weights, k, m, deg, True, rational)
        raise Unsupported(f"B-curve knot/pole mismatch: sum(mults)={total} nv={nv} deg={deg}")

    def curve2d_bspline(self, nc) -> Geom2d_BSplineCurve:
        deg, nv, dim, rational, periodic, verts, knots, mults = self._nurbs_curve_data(nc)
        poles = TColgp_Array1OfPnt2d(1, nv)
        weights = TColStd_Array1OfReal(1, nv)
        for i in range(nv):
            row = verts[i * dim:(i + 1) * dim]
            if rational:
                w = row[-1]; x, y = row[0] / w, row[1] / w
            else:
                w = 1.0; x, y = row[0], row[1]
            poles.SetValue(i + 1, gp_Pnt2d(x, y)); weights.SetValue(i + 1, w)
        k = TColStd_Array1OfReal(1, len(knots)); m = TColStd_Array1OfInteger(1, len(mults))
        for i, (kk, mm) in enumerate(zip(knots, mults)):
            k.SetValue(i + 1, kk); m.SetValue(i + 1, mm)
        total = sum(mults)
        if total == nv + deg + 1:
            c = Geom2d_BSplineCurve(poles, weights, k, m, deg, False)
            if mults[0] < deg + 1 or mults[-1] < deg + 1:
                flat = [kk for kk, mm in zip(knots, mults) for _ in range(mm)]
                c.Segment(flat[deg], flat[len(flat) - 1 - deg])
            return c
        if periodic and total - mults[-1] == nv:
            return Geom2d_BSplineCurve(poles, weights, k, m, deg, True)
        raise Unsupported("2D B-curve knot/pole mismatch")

    def surface_bspline(self, ns) -> Geom_BSplineSurface:
        v = ns.values
        ud, vd = v["u_degree"], v["v_degree"]
        nu, nvv = v["n_u_vertices"], v["n_v_vertices"]
        dim = v["vertex_dim"]; rational = bool(v.get("rational"))
        up, vp = bool(v.get("u_periodic")), bool(v.get("v_periodic"))
        verts = _field(self.by[v["bspline_vertices"]].values, "vertices")
        um = [int(x) for x in _field(self.by[v["u_knot_mult"]].values, "mult")]
        vm = [int(x) for x in _field(self.by[v["v_knot_mult"]].values, "mult")]
        uk = [float(x) for x in _field(self.by[v["u_knots"]].values, "knots")]
        vk = [float(x) for x in _field(self.by[v["v_knots"]].values, "knots")]
        poles = TColgp_Array2OfPnt(1, nu, 1, nvv)
        weights = TColStd_Array2OfReal(1, nu, 1, nvv)
        for i in range(nu):
            for j in range(nvv):
                idx = i * nvv + j          # v index varies fastest
                row = verts[idx * dim:(idx + 1) * dim]
                if rational:
                    w = row[-1]; xyz = [c / w for c in row[:-1]]
                else:
                    w = 1.0; xyz = list(row)
                poles.SetValue(i + 1, j + 1, gp_Pnt(xyz[0] * SCALE, xyz[1] * SCALE, xyz[2] * SCALE))
                weights.SetValue(i + 1, j + 1, w)

        def arr(vals, integer=False):
            a = TColStd_Array1OfInteger(1, len(vals)) if integer else TColStd_Array1OfReal(1, len(vals))
            for i, x in enumerate(vals):
                a.SetValue(i + 1, x)
            return a

        def periodic_ok(mults, n, deg, per):
            tot = sum(mults)
            if tot == n + deg + 1:
                return False
            if per and tot - mults[-1] == n:
                return True
            raise Unsupported(f"B-surface knot/pole mismatch tot={tot} n={n} deg={deg}")

        uper = periodic_ok(um, nu, ud, up)
        vper = periodic_ok(vm, nvv, vd, vp)
        s = Geom_BSplineSurface(poles, weights, arr(uk), arr(vk), arr(um, True), arr(vm, True),
                                ud, vd, uper, vper)
        # clamp unclamped directions to the defined range
        u1, u2, v1, v2 = s.Bounds()
        if not uper and (um[0] < ud + 1 or um[-1] < ud + 1):
            flat = [kk for kk, mm in zip(uk, um) for _ in range(mm)]
            u1, u2 = flat[ud], flat[len(flat) - 1 - ud]
        if not vper and (vm[0] < vd + 1 or vm[-1] < vd + 1):
            flat = [kk for kk, mm in zip(vk, vm) for _ in range(mm)]
            v1, v2 = flat[vd], flat[len(flat) - 1 - vd]
        b = s.Bounds()
        if (u1, u2, v1, v2) != b:
            s.Segment(u1, u2, v1, v2)
        return s

    # ---------------------------------------------------------------- curves
    def curve(self, idx) -> Geom_Curve:
        if idx in self._curves:
            return self._curves[idx]
        nd = self.by[idx]
        v = nd.values
        name = nd.name
        if name == "LINE":
            c = Geom_Line(P(v["pvec"]), D(v["direction"]))
            # Parasolid line parameter is arc length in metres; OCCT in mm. The
            # parametrisation only matters for pcurves/SP-curves (never on lines).
        elif name == "CIRCLE":
            c = Geom_Circle(gp_Ax2(P(v["centre"]), D(v["normal"]), D(v["x_axis"])), v["radius"] * SCALE)
        elif name == "ELLIPSE":
            a, b = v["major_radius"] * SCALE, v["minor_radius"] * SCALE
            ax = gp_Ax2(P(v["centre"]), D(v["normal"]), D(v["x_axis"]))
            if a >= b:
                c = Geom_Ellipse(ax, a, b)
            else:
                # OCCT wants major >= minor: rotate x axis by 90 degrees.
                n = D(v["normal"]); x = D(v["x_axis"])
                y = n.Crossed(x)
                c = Geom_Ellipse(gp_Ax2(P(v["centre"]), n, y), b, a)
        elif name == "B_CURVE":
            c = self.curve_bspline(self.by[v["nurbs"]])
        elif name == "TRIMMED_CURVE":
            c = self.curve(v["basis_curve"])
        elif name == "INTERSECTION":
            c = self.curve_intersection(nd)
        elif name == "SP_CURVE":
            c = self.curve_sp_3d(nd)
        else:
            raise Unsupported(f"curve type {name}")
        self._curves[idx] = c
        return c

    def curve_intersection(self, nd) -> Geom_Curve:
        v = nd.values
        chart = self.by[v["chart"]].values
        h = chart.get("hvec") or []
        if not isinstance(h, list):
            h = [h]
        pts = [gp_Pnt(h[i] * SCALE, h[i + 1] * SCALE, h[i + 2] * SCALE) for i in range(0, len(h), 3)]
        # a terminator ('T') limit holds the exact singular end point, which lies
        # just outside the chart; an arbitrary limit ('L') may too. Include them.
        for key, at_end in (("start", False), ("end", True)):
            lidx = v.get(key, 0)
            if not lidx:
                continue
            lv = self.by[lidx].values
            if _ch(lv.get("type", 72)) not in ("T", "L"):
                continue
            hv = lv.get("hvec") or []
            if not isinstance(hv, list) or len(hv) < 3:
                continue
            q = gp_Pnt(hv[0] * SCALE, hv[1] * SCALE, hv[2] * SCALE)
            ref = pts[-1] if at_end else pts[0]
            if q.Distance(ref) > PREC * 10:
                pts = pts + [q] if at_end else [q] + pts
        # drop consecutive duplicates
        clean = [pts[0]]
        for p in pts[1:]:
            if p.Distance(clean[-1]) > PREC:
                clean.append(p)
        pts = clean
        closed = len(pts) > 3 and pts[0].Distance(pts[-1]) <= PREC * 10
        if closed:
            pts = pts[:-1]
        if len(pts) < 2:
            raise Unsupported("intersection chart with <2 points")
        exact = self._intersect_exact(v["surface"], pts, closed)
        if exact is not None:
            return exact
        self.warn("intersection curve interpolated through chart points")
        arr = TColgp_HArray1OfPnt(1, len(pts))
        for i, p in enumerate(pts):
            arr.SetValue(i + 1, p)
        interp = GeomAPI_Interpolate(arr, closed, PREC)
        interp.Perform()
        if not interp.IsDone():
            raise Unsupported("intersection interpolation failed")
        return interp.Curve()

    def _intersect_exact(self, surf_idx, pts, closed):
        """Surface/surface intersection in OCCT; the chart picks the branch and
        the direction. None when no branch follows the chart closely."""
        names = [self.by[i].name for i in surf_idx]
        dbg = os.environ.get("DEBUG_ISS")
        if any(n in ("BLENDED_EDGE", "BLEND_BOUND") for n in names):
            return None
        if any(i in getattr(self, "_building", ()) for i in surf_idx):
            if dbg: print("[iss] surface under construction", names)
            return None
        try:
            s1, _ = self.surface(surf_idx[0])
            s2, _ = self.surface(surf_idx[1])
        except Exception as ex:
            if dbg: print("[iss] surface failed", names, ex)
            return None
        if getattr(self._surfs[surf_idx[0]][1], "approx", False) or getattr(self._surfs[surf_idx[1]][1], "approx", False):
            return None
        if (s1.DynamicType().Name() in _QUADRICS_V and s2.DynamicType().Name() in _QUADRICS_V):
            # cylinder x cone on the untrimmed (infinite) surfaces takes seconds
            # per call in OCCT; bound both along v around the chart, with room
            # for the whole intersection loop.  Same branches, ~6x faster.
            r1, r2 = _ref_radius(s1), _ref_radius(s2)
            w1, w2 = _v_window(s1, pts, r2), _v_window(s2, pts, r1)
            if w1 is not None and w2 is not None:
                s1, s2 = w1, w2
        try:
            iss = GeomAPI_IntSS(s1, s2, PREC)
        except Exception as ex:
            if dbg: print("[iss] IntSS exception", names, ex)
            return None
        if not iss.IsDone() or iss.NbLines() == 0:
            if dbg: print("[iss] no lines", names)
            return None
        best = None
        dbg = os.environ.get("DEBUG_ISS")
        for i in range(1, iss.NbLines() + 1):
            c = iss.Line(i)
            dmax = 0.0
            params = []
            ok = True
            if dbg:
                print(f"[iss] {names} line {i} {c.DynamicType().Name()}")
            for p in pts:
                try:
                    u, d = self._project(c, p)
                except Exception:
                    ok = False
                    break
                dmax = max(dmax, d)
                params.append(u)
            if dbg:
                print(f"[iss]   ok={ok} dmax={dmax:.3g} params={params[:4]}")
            if not ok or dmax > 5e-3:
                continue
            if best is None or dmax < best[0]:
                best = (dmax, c, params)
        if best is None:
            return None
        dmax, c, params = best
        if c.IsClosed() and not c.IsPeriodic():
            # closed but clamped: re-interpolate densely as a periodic curve so
            # edges may wrap around the seam
            f, l = c.FirstParameter(), c.LastParameter()
            n = 64
            arr = TColgp_HArray1OfPnt(1, n)
            for i in range(n):
                arr.SetValue(i + 1, c.Value(f + (l - f) * i / n))
            interp = GeomAPI_Interpolate(arr, True, PREC)
            interp.Perform()
            if not interp.IsDone():
                return None
            c = interp.Curve()
            params = [self._project(c, p)[0] for p in pts]
        # direction: chart order == increasing parameter
        if len(params) >= 2:
            inc = sum(1 for a, b in zip(params, params[1:]) if b > a)
            dec = len(params) - 1 - inc
            if c.IsPeriodic():
                per = c.Period()
                inc = sum(1 for a, b in zip(params, params[1:]) if (b - a) % per < per / 2)
                dec = len(params) - 1 - inc
            if dec > inc:
                c = c.Reversed()
        return c

    def curve_sp_2d(self, nd) -> tuple[Geom2d_Curve, Geom_Surface]:
        """2D curve in OCCT parameter space of the surface, and that surface."""
        v = nd.values
        surf, pm = self.surface(v["surface"])
        bc = self.by[v["b_curve"]]
        c2 = self.curve2d_bspline(self.by[bc.values["nurbs"]])
        if not pm.is_identity():
            g = gp_GTrsf2d()
            g.SetVectorialPart(gp_Mat2d(gp_XY(pm.a, pm.c), gp_XY(pm.b, pm.d)))
            g.SetTranslationPart(gp_XY(pm.e, pm.f))
            # apply to poles
            n = c2.NbPoles()
            for i in range(1, n + 1):
                p = c2.Pole(i)
                xy = gp_XY(p.X(), p.Y())
                g.Transforms(xy)
                c2.SetPole(i, gp_Pnt2d(xy.X(), xy.Y()))
        return c2, surf

    def curve_sp_3d(self, nd) -> Geom_Curve:
        if self.by[nd.values["surface"]].name == "BLENDED_EDGE":
            return self.curve_sp_on_blend(nd)
        c2, surf = self.curve_sp_2d(nd)
        return self._curve3d_from_pcurve(c2, surf)

    def blend_point(self, bidx, u, vfrac) -> gp_Pnt:
        """Evaluate a rolling-ball blend at Parasolid (u = spine param, v in [0,1])."""
        if bidx not in self._blend_eval:
            self.surface(bidx)
        be = self._blend_eval[bidx]
        spine = be["spine"]
        if be["spine_name"] == "LINE":
            uo = u * SCALE
        elif be["spine_name"] in ("CIRCLE", "ELLIPSE", "B_CURVE"):
            uo = u
        else:
            # chart parametrisation ~ arc length (metres) from the first chart point
            from OCP.GCPnts import GCPnts_AbscissaPoint
            from OCP.GeomAdaptor import GeomAdaptor_Curve
            ad = GeomAdaptor_Curve(spine)
            uo = GCPnts_AbscissaPoint(ad, (u - be["t0"]) * SCALE, be["u0"]).Parameter()
        c = spine.Value(uo)
        p1 = be["contact"](c, 0)
        p2 = be["contact"](c, 1)
        if be["flip_v"]:
            p1, p2 = p2, p1
        e1 = gp_Vec(c, p1); rr = e1.Magnitude(); e1.Normalize()
        e2 = gp_Vec(c, p2); e2 = e2 - e1 * e2.Dot(e1)
        if e2.Magnitude() < 1e-9:
            raise Unsupported("blend degenerate section")
        e2.Normalize()
        ang = math.acos(max(-1.0, min(1.0, gp_Vec(c, p1).Normalized().Dot(gp_Vec(c, p2).Normalized()))))
        t = ang * vfrac
        return gp_Pnt(c.XYZ() + (e1 * (rr * math.cos(t)) + e2 * (rr * math.sin(t))).XYZ())

    def curve_sp_on_blend(self, nd) -> Geom_Curve:
        bidx = nd.values["surface"]
        bc = self.by[nd.values["b_curve"]]
        c2 = self.curve2d_bspline(self.by[bc.values["nurbs"]])
        a, b = c2.FirstParameter(), c2.LastParameter()
        n = 25
        pts = []
        for i in range(n):
            q = c2.Value(a + (b - a) * i / (n - 1))
            pts.append(self.blend_point(bidx, q.X(), q.Y()))
        clean = [pts[0]]
        for p in pts[1:]:
            if p.Distance(clean[-1]) > PREC:
                clean.append(p)
        if len(clean) < 2:
            raise Unsupported("SP-curve on blend degenerate")
        arr = TColgp_HArray1OfPnt(1, len(clean))
        for i, p in enumerate(clean):
            arr.SetValue(i + 1, p)
        interp = GeomAPI_Interpolate(arr, False, PREC)
        interp.Perform()
        if not interp.IsDone():
            raise Unsupported("SP-curve on blend interpolation failed")
        return interp.Curve()

    @staticmethod
    def _curve3d_from_pcurve(c2, surf) -> Geom_Curve:
        from OCP.BRepAdaptor import BRepAdaptor_Curve
        e = BRepBuilderAPI_MakeEdge(c2, surf).Edge()
        BRepLib.BuildCurves3d_s(e, PREC, GeomAbs_C2, 14, 200)
        ad = BRepAdaptor_Curve(e)
        c = ad.Curve().Curve()
        if c is None:
            raise Unsupported("no 3D curve from SP-curve")
        return c

    def curve_sense(self, cidx) -> bool:
        """Effective direction of the curve node w.r.t. the OCCT curve parameter."""
        plus = True
        while cidx:
            cn = self.by[cidx]
            plus = (plus == _plus(cn.values.get("sense", 43)))
            if cn.name == "TRIMMED_CURVE":
                cidx = cn.values["basis_curve"]
            else:
                break
        return plus

    # -------------------------------------------------------------- surfaces
    def surface(self, idx) -> tuple[Geom_Surface, ParamMap]:
        if idx in self._surfs:
            return self._surfs[idx]
        if not hasattr(self, "_building"):
            self._building = set()
        if idx in self._building:
            raise Unsupported(f"surface {idx} needed while being built")
        self._building.add(idx)
        try:
            return self._surface_impl(idx)
        finally:
            self._building.discard(idx)

    def _surface_impl(self, idx) -> tuple[Geom_Surface, ParamMap]:
        nd = self.by[idx]
        v = nd.values
        name = nd.name
        pm = ParamMap()
        if name == "PLANE":
            s = Geom_Plane(gp_Ax3(P(v["pvec"]), D(v["normal"]), D(v["x_axis"])))
            pm = ParamMap(a=SCALE, d=SCALE)       # u,v are lengths (m -> mm)
        elif name == "CYLINDER":
            s = Geom_CylindricalSurface(gp_Ax3(P(v["pvec"]), D(v["axis"]), D(v["x_axis"])), v["radius"] * SCALE)
            pm = ParamMap(d=SCALE)                # v is a length
        elif name == "CONE":
            sa, ca = v["sin_half_angle"], v["cos_half_angle"]
            ang = math.atan2(sa, ca)
            s = Geom_ConicalSurface(gp_Ax3(P(v["pvec"]), D(v["axis"]), D(v["x_axis"])), ang, v["radius"] * SCALE)
            # Measured on SolidWorks data (the 2006 XT reference has the axis sign
            # inverted): R = P + vA + (Xcos u + Ysin u)(r + v tan a), Y = A x X.
            # OCCT: P + (r + v' sin a)(cos u X + sin u Y) + v' cos a A  =>  v' = v / cos a
            pm = ParamMap(d=SCALE / ca)
        elif name == "SPHERE":
            s = Geom_SphericalSurface(gp_Ax3(P(v["centre"]), D(v["axis"]), D(v["x_axis"])), v["radius"] * SCALE)
        elif name == "TORUS":
            R, r = v["major_radius"] * SCALE, v["minor_radius"] * SCALE
            if R > 0:
                s = Geom_ToroidalSurface(gp_Ax3(P(v["centre"]), D(v["axis"]), D(v["x_axis"])), R, r)
            else:
                # lemon torus (a < 0): same point set as the torus (|a|, b) about -A,
                # with u' = -(u + pi), v' = v + pi; the Jacobian is negative.
                s = Geom_ToroidalSurface(gp_Ax3(P(v["centre"]), D(v["axis"]).Reversed(), D(v["x_axis"])), -R, r)
                pm = ParamMap(a=-1.0, e=-math.pi, f=math.pi, flip=True)
        elif name == "B_SURFACE":
            s = self.surface_bspline(self.by[v["nurbs"]])
        elif name == "SWEPT_SURF":
            sec = self.curve(v["section"])
            sw = v["sweep"]
            length = math.sqrt(sum(c * c for c in sw))
            s = Geom_SurfaceOfLinearExtrusion(sec, D(sw))
            pm = ParamMap(d=SCALE * length)
            if self.by[v["section"]].name == "LINE":
                pm.a = SCALE
        elif name == "SPUN_SURF":
            prof = self.curve(v["profile"])
            s = Geom_SurfaceOfRevolution(prof, gp_Ax1(P(v["base"]), D(v["axis"])))
            # Parasolid (u = profile param, v = angle); OCCT (u = angle, v = profile param)
            pu = SCALE if self.by[v["profile"]].name == "LINE" else 1.0
            pm = ParamMap(a=0.0, b=1.0, c=pu, d=0.0, flip=True)
        elif name == "OFFSET_SURF":
            base, bpm = self.surface(v["surface"])
            bsense = _plus(self.by[v["surface"]].values.get("sense", 43))
            off = v["offset"] * SCALE * (1.0 if bsense else -1.0)
            if bpm.flip:
                off = -off
            s = Geom_OffsetSurface(base, off)
            pm = ParamMap(bpm.a, bpm.b, bpm.c, bpm.d, bpm.e, bpm.f, bpm.flip)
        elif name == "BLENDED_EDGE":
            s = self.surface_blend(nd)
            # our u follows the spine direction and v runs from support 0 to
            # support 1; Parasolid's v runs the other way when the spine sense
            # is negative, which flips the natural normal.
            flip = not self.curve_sense(v["spine"])
            if os.environ.get("BLEND_FLIP") in ("0", "1"):
                flip = os.environ["BLEND_FLIP"] == "1"
            pm = ParamMap(flip=flip)
            pm.approx = True
        else:
            raise Unsupported(f"surface type {name}")
        self._surfs[idx] = (s, pm)
        return s, pm

    def surface_blend(self, nd) -> Geom_Surface:
        """Rolling-ball blend approximated by sampling circular cross-sections."""
        v = nd.values
        supports = []
        for sidx in v["surface"]:
            sn = self.by[sidx]
            if sn.name == "BLENDED_EDGE" and all(abs(x) < 1e-12 for x in sn.values["range"]):
                supports.append(("curve", self.curve(sn.values["spine"])))   # cliff edge
            else:
                supports.append(("surf", self.surface(sidx)[0]))
        spine = self.curve(v["spine"])
        r1, r2 = v["range"]
        r = max(abs(r1), abs(r2)) * SCALE
        u1, u2 = spine.FirstParameter(), spine.LastParameter()
        # restrict the spine range to what the owning faces actually use
        params = []
        for fidx in self.faces_of_surface(nd.index):
            for l in self.chain(self.by[fidx].values.get("loop", 0), "next"):
                for f in self.chain(self.by[l].values["halfedge"], "forward"):
                    vidx = self.by[f].values.get("vertex", 0)
                    if vidx:
                        pt = P(self.by[self.by[vidx].values["point"]].values["pvec"])
                        try:
                            params.append(self._project(spine, pt)[0])
                        except Unsupported:
                            pass
        if params:
            lo, hi = min(params), max(params)
            span = max(hi - lo, r)
            lo, hi = lo - 0.05 * span, hi + 0.05 * span
            if spine.IsPeriodic() and hi - lo > spine.Period():
                lo, hi = lo, lo + spine.Period()
            u1, u2 = max(u1, lo) if math.isfinite(u1) and abs(u1) < 1e50 else lo, min(u2, hi) if math.isfinite(u2) and abs(u2) < 1e50 else hi
        if not (math.isfinite(u1) and math.isfinite(u2)) or abs(u1) > 1e50 or abs(u2) > 1e50:
            raise Unsupported("blend with unbounded spine and no owning face")
        # boundary curves of the owning faces, split by which support they lie on
        rails = ([], [])
        for fidx in self.faces_of_surface(nd.index):
            for l in self.chain(self.by[fidx].values.get("loop", 0), "next"):
                for f in self.chain(self.by[l].values["halfedge"], "forward"):
                    e = self.by[f].values.get("edge", 0)
                    if not e:
                        continue
                    ev = self.by[e].values
                    cidx = ev.get("curve", 0)
                    if not cidx:
                        continue
                    cn = self.by[cidx]
                    base = cn
                    while base.name == "TRIMMED_CURVE":
                        base = self.by[base.values["basis_curve"]]
                    if base.name == "SP_CURVE":
                        continue          # would recurse into this blend
                    if base.name == "CIRCLE" and abs(base.values["radius"] * SCALE - r) < 1e-3:
                        continue          # end arc of the blend
                    try:
                        cv = self.curve(cidx)
                    except Exception:
                        continue
                    # which support is this rail on? test the curve midpoint
                    s_idx, t_idx = self.edge_vertices(e)
                    if s_idx is None or t_idx is None:
                        continue
                    pa = P(self.by[self.by[s_idx].values["point"]].values["pvec"])
                    pb = P(self.by[self.by[t_idx].values["point"]].values["pvec"])
                    try:
                        ua, _ = self._project(cv, pa); ub, _ = self._project(cv, pb)
                    except Unsupported:
                        continue
                    mid = cv.Value(0.5 * (ua + ub))
                    d = [mid.Distance(self._support_foot(mid, sp_)) for sp_ in supports]
                    side = 0 if d[0] < d[1] else 1
                    rails[side].append((cv, min(ua, ub), max(ua, ub)))

        def contact(c, side):
            best = None
            for cv, ua, ub in rails[side]:
                try:
                    u, d = self._project(cv, c)
                except Unsupported:
                    continue
                u = min(max(u, ua), ub)
                q = cv.Value(u)
                dd = c.Distance(q)
                if best is None or abs(dd - r) < abs(best[0] - r):
                    best = (dd, q)
            if best is not None and abs(best[0] - r) < 0.2 * r + 1e-3:
                return best[1]
            return self._support_foot(c, supports[side])

        # remember how to evaluate this blend at (spine param, angle fraction)
        spine_node = self.by[v["spine"]]
        chart_t0 = 0.0
        if spine_node.name == "INTERSECTION":
            chart_t0 = self.by[spine_node.values["chart"]].values.get("base_parameter", 0.0)
            hv = self.by[spine_node.values["chart"]].values.get("hvec") or []
            p0 = gp_Pnt(hv[0] * SCALE, hv[1] * SCALE, hv[2] * SCALE)
            try:
                chart_u0 = self._project(spine, p0)[0]
            except Unsupported:
                chart_u0 = spine.FirstParameter()
        else:
            chart_u0 = None
        spine_sense_plus = self.curve_sense(v["spine"])
        self._blend_eval[nd.index] = dict(spine=spine, spine_name=spine_node.name, t0=chart_t0, u0=chart_u0,
                                          contact=contact, r=r, flip_v=not spine_sense_plus)
        nu, nv = 40, 12
        grid = TColgp_Array2OfPnt(1, nu, 1, nv)
        if os.environ.get("DEBUG_BLEND"):
            print(f"[blend {nd.index}] type={_ch(v['blend_type'])} r={r:.4f} spine={self.by[v['spine']].name} u=[{u1:.4g},{u2:.4g}] rails={[len(x) for x in rails]} supports={[k for k,_ in supports]} faces={self.faces_of_surface(nd.index)}")
        sections = []
        for i in range(nu):
            u = u1 + (u2 - u1) * i / (nu - 1)
            c = spine.Value(u)
            p1 = contact(c, 0)
            p2 = contact(c, 1)
            if os.environ.get("DEBUG_BLEND") and i in (0, nu // 2, nu - 1):
                print(f"   u={u:.4g} c=({c.X():.3f},{c.Y():.3f},{c.Z():.3f}) |c-p1|={c.Distance(p1):.4f} |c-p2|={c.Distance(p2):.4f}")
            e1 = gp_Vec(c, p1); e2 = gp_Vec(c, p2)
            rr = e1.Magnitude()
            if rr < PREC:
                raise Unsupported("blend degenerate section")
            e1.Normalize()
            e2 = e2 - e1 * (e2.Dot(e1))
            if e2.Magnitude() < 1e-6:
                sections.append([c, e1, rr, None, 0.0])     # vanishing section
            else:
                e2.Normalize()
                ang = math.acos(max(-1.0, min(1.0, gp_Vec(c, p1).Normalized().Dot(gp_Vec(c, p2).Normalized()))))
                sections.append([c, e1, rr, e2, ang])
        valid = [i for i, sct in enumerate(sections) if sct[3] is not None]
        if not valid:
            raise Unsupported("blend degenerate section")
        for i, sct in enumerate(sections):
            if sct[3] is None:
                j = min(valid, key=lambda k: abs(k - i))
                sct[3] = sections[j][3]
        for i, (c, e1, rr, e2, ang) in enumerate(sections):
            for j in range(nv):
                t = ang * j / (nv - 1)
                q = gp_Pnt(c.XYZ() + (e1 * (rr * math.cos(t)) + e2 * (rr * math.sin(t))).XYZ())
                grid.SetValue(i + 1, j + 1, q)
        box = Bnd_Box()
        for i in range(1, nu + 1):
            for j in range(1, nv + 1):
                box.Add(grid.Value(i, j))
        box.Enlarge(3.0 * r + 0.5)

        def poles_inside(sf):
            pl = sf.Poles()
            for i in range(pl.LowerRow(), pl.UpperRow() + 1):
                for j in range(pl.LowerCol(), pl.UpperCol() + 1):
                    if box.IsOut(pl.Value(i, j)):
                        return False
            return True

        surf = None
        attempts = [
            lambda: GeomAPI_PointsToBSplineSurface(grid, 3, 8, GeomAbs_C2, 1e-4),
            lambda: GeomAPI_PointsToBSplineSurface(grid, 3, 3, GeomAbs_C2, 1e-3),
            lambda: GeomAPI_PointsToBSplineSurface(grid, 1.0, 1.0, 1.0, 3, GeomAbs_C2, 1e-3),   # smoothing fit
        ]
        for make in attempts:
            try:
                approx = make()
            except Exception:
                continue
            if not approx.IsDone():
                continue
            cand = approx.Surface()
            if poles_inside(cand):
                surf = cand
                break
        if surf is None:
            raise Unsupported("blend approximation diverged")
        self.warn("rolling-ball blend approximated by B-spline")
        return surf

    def _support_foot(self, c, support):
        kind, geom = support
        if kind == "surf":
            return _foot(c, geom)
        u, d = self._project(geom, c)
        return geom.Value(u)

    # -------------------------------------------------------------- topology
    def vertex(self, idx) -> TopoDS_Vertex:
        if idx in self._verts:
            return self._verts[idx]
        vn = self.by[idx].values
        pt = self.by[vn["point"]].values["pvec"]
        tol = vn.get("tolerance")
        t = PREC if _is_null(tol) else max(PREC, tol * SCALE)
        V = TopoDS_Vertex()
        self.builder.MakeVertex(V, P(pt), t)
        self._verts[idx] = V
        return V

    def edge_vertices(self, e_idx):
        """(start_vertex_idx, end_vertex_idx) from the fin ring; None for ring edges."""
        ev = self.by[e_idx].values
        fin = ev["halfedge"]
        fins = self.chain(fin, "other")
        pos = [f for f in fins if _plus(self.by[f].values.get("sense", 43))]
        neg = [f for f in fins if not _plus(self.by[f].values.get("sense", 43))]
        end = self.by[pos[0]].values.get("vertex", 0) if pos else 0
        start = self.by[neg[0]].values.get("vertex", 0) if neg else 0
        return (start or None), (end or None)

    def edge_curve_and_map(self, e_idx):
        """Return (Geom_Curve|None, (c2d, surf)|None) for an edge."""
        ev = self.by[e_idx].values
        cidx = ev.get("curve", 0)
        if cidx:
            cn = self.by[cidx]
            base = cn
            if cn.name == "TRIMMED_CURVE":
                base = self.by[cn.values["basis_curve"]]
            if base.name == "SP_CURVE":
                if self.by[base.values["surface"]].name == "BLENDED_EDGE":
                    return self.curve_sp_on_blend(base), None
                return None, self.curve_sp_2d(base)
            return self.curve(cidx), None
        # tolerant edge: use a fin curve (trimmed SP curve)
        for f in self.chain(ev["halfedge"], "other"):
            fc = self.by[f].values.get("curve", 0)
            if fc:
                cn = self.by[fc]
                if cn.name == "TRIMMED_CURVE":
                    cn = self.by[cn.values["basis_curve"]]
                if cn.name == "SP_CURVE":
                    if self.by[cn.values["surface"]].name == "BLENDED_EDGE":
                        continue      # prefer the other fin's curve
                    return None, self.curve_sp_2d(cn)
                return self.curve(cn.index), None
        for f in self.chain(ev["halfedge"], "other"):
            fc = self.by[f].values.get("curve", 0)
            if fc:
                cn = self.by[fc]
                if cn.name == "TRIMMED_CURVE":
                    cn = self.by[cn.values["basis_curve"]]
                if cn.name == "SP_CURVE":
                    return self.curve_sp_on_blend(cn), None
        raise Unsupported(f"edge {e_idx} has no curve")

    def edge(self, e_idx) -> TopoDS_Edge:
        if e_idx in self._edges:
            return self._edges[e_idx]
        ev = self.by[e_idx].values
        c3, sp = self.edge_curve_and_map(e_idx)
        s_idx, t_idx = self.edge_vertices(e_idx)
        cidx = ev.get("curve", 0)
        sense_plus = self.curve_sense(cidx) if cidx else self._fin_curve_sense(ev)
        if sp is not None:
            c2d, surf = sp
            c3 = self._curve3d_from_pcurve(c2d, surf)
        if s_idx is None or t_idx is None:
            # ring edge: full closed curve
            e = BRepBuilderAPI_MakeEdge(c3).Edge()
            self._edges[e_idx] = e
            self._edge_closed[e_idx] = True
            self._edge_sense[e_idx] = sense_plus
            return e
        Vs, Vt = self.vertex(s_idx), self.vertex(t_idx)
        Ps, Pt = BRep_Tool.Pnt_s(Vs), BRep_Tool.Pnt_s(Vt)
        ps, ds = self._project(c3, Ps)
        pt, dt = self._project(c3, Pt)
        ckind = "endpoint_on_" + (("SP@" + self.by[self.by[self._sp_surface_of(ev)].values["surface"]].name) if sp is not None else self.by[cidx].name if cidx else "?")
        self.rep.check(ckind, max(ds, dt))
        for V, d in ((Vs, ds), (Vt, dt)):
            if d > BRep_Tool.Tolerance_s(V):
                self.builder.UpdateVertex(V, d * 1.05 + PREC)
        # OCCT edge runs in increasing curve parameter.
        if sense_plus:
            V1, V2, p1, p2 = Vs, Vt, ps, pt
        else:
            V1, V2, p1, p2 = Vt, Vs, pt, ps
        closed_same = (s_idx == t_idx)
        if not c3.IsPeriodic() and not closed_same:
            geo_plus = pt > ps
            if geo_plus != sense_plus:
                # the vertex order is authoritative for the edge direction
                if os.environ.get("DEBUG_SENSE"):
                    print(f"[sense] edge {e_idx} curve={self.by[cidx].name if cidx else 'fin'} {c3.DynamicType().Name()} range=[{c3.FirstParameter():.4g},{c3.LastParameter():.4g}] ps={ps:.5g} pt={pt:.5g} ds={ds:.2e} dt={dt:.2e} flag_plus={sense_plus} Ps=({Ps.X():.3f},{Ps.Y():.3f},{Ps.Z():.3f}) Pt=({Pt.X():.3f},{Pt.Y():.3f},{Pt.Z():.3f})")
                sense_plus = geo_plus
                self.rep.check("sense_corrected_by_geometry", 1.0, 0.0)
                if sense_plus:
                    V1, V2, p1, p2 = Vs, Vt, ps, pt
                else:
                    V1, V2, p1, p2 = Vt, Vs, pt, ps
        self._edge_sense[e_idx] = sense_plus
        if c3.IsPeriodic():
            per = c3.Period()
            if closed_same:
                p2 = p1 + per
            else:
                while p2 <= p1 + 1e-9:
                    p2 += per
                # choose the shorter consistent branch is NOT correct in general;
                # Parasolid edges follow curve direction, so p2 > p1 is the rule.
        else:
            if closed_same:
                p1, p2 = c3.FirstParameter(), c3.LastParameter()
            elif p2 < p1:
                # projection landed on the wrong side of a closed non-periodic curve?
                raise Unsupported(f"edge {e_idx}: parameters reversed on open curve")
        mk = BRepBuilderAPI_MakeEdge(c3, V1, V2, p1, p2)
        if not mk.IsDone():
            raise Unsupported(f"edge {e_idx}: MakeEdge failed ({mk.Error()})")
        e = mk.Edge()
        self._edges[e_idx] = e
        self._edge_closed[e_idx] = closed_same
        return e

    def _sp_surface_of(self, ev) -> int:
        """Index of the SP_CURVE node behind an edge (edge.curve or a fin curve)."""
        cands = [ev.get("curve", 0)] + [self.by[f].values.get("curve", 0) for f in self.chain(ev["halfedge"], "other")]
        for c in cands:
            while c:
                cn = self.by[c]
                if cn.name == "SP_CURVE":
                    return c
                if cn.name == "TRIMMED_CURVE":
                    c = cn.values["basis_curve"]
                else:
                    break
        return 0

    def _fin_curve_sense(self, ev) -> bool:
        for f in self.chain(ev["halfedge"], "other"):
            fc = self.by[f].values.get("curve", 0)
            if fc:
                return self.curve_sense(fc)
        return True

    @staticmethod
    def _project(c, pnt):
        """Parameter of the point on the curve nearest to pnt, considering the
        curve ends too (extrema search misses points exactly at an end)."""
        best = None
        f0, l0 = c.FirstParameter(), c.LastParameter()
        ranges = [(f0, l0)] if (abs(f0) < 1e50 and abs(l0) < 1e50) else [(-50.0, 50.0), (-1e4, 1e4), (-1e7, 1e7)]
        for (f, l) in ranges:
            try:
                proj = GeomAPI_ProjectPointOnCurve(pnt, c, f, l)
            except Exception:
                continue
            if proj.NbPoints() > 0:
                best = (proj.LowerDistance(), proj.LowerDistanceParameter())
                break
        for u in (c.FirstParameter(), c.LastParameter()):
            if math.isfinite(u) and abs(u) < 1e50:
                try:
                    d = pnt.Distance(c.Value(u))
                except Exception:
                    continue
                if best is None or d < best[0] - 1e-12:
                    best = (d, u)
        if best is None:
            raise Unsupported("vertex projection failed")
        return best[1], best[0]

    def _add_sampled_pcurve(self, e, face, surf):
        """pcurve by sampling the 3D curve and inverting with ShapeAnalysis_Surface,
        which stays stable next to degenerate rows of an approximated surface."""
        from OCP.ShapeAnalysis import ShapeAnalysis_Surface
        from OCP.BRepAdaptor import BRepAdaptor_Curve
        from OCP.TColgp import TColgp_HArray1OfPnt2d
        from OCP.Geom2dAPI import Geom2dAPI_Interpolate
        ad = BRepAdaptor_Curve(e)
        f, l = ad.FirstParameter(), ad.LastParameter()
        sas = ShapeAnalysis_Surface(surf)
        n = 24
        uv = []
        prev = None
        for i in range(n):
            t = f + (l - f) * i / (n - 1)
            p = ad.Value(t)
            q = sas.ValueOfUV(p, PREC) if prev is None else sas.NextValueOfUV(prev, p, PREC)
            prev = q
            uv.append(q)
        arr = TColgp_HArray1OfPnt2d(1, n)
        for i, q in enumerate(uv):
            arr.SetValue(i + 1, q)
        interp = Geom2dAPI_Interpolate(arr, False, 1e-9)
        interp.Perform()
        if not interp.IsDone():
            raise Unsupported("pcurve sampling failed")
        c2 = interp.Curve()
        # parametrise like the 3D curve
        from OCP.TColStd import TColStd_Array1OfReal
        # Geom2dAPI_Interpolate uses chord-length parameters; re-map the 3D edge
        # range onto them by rebuilding the 2D curve with the 3D parameters.
        from OCP.TColStd import TColStd_HArray1OfReal
        params = TColStd_HArray1OfReal(1, n)
        for i in range(n):
            params.SetValue(i + 1, f + (l - f) * i / (n - 1))
        interp = Geom2dAPI_Interpolate(arr, params, False, 1e-9)
        interp.Perform()
        if interp.IsDone():
            c2 = interp.Curve()
        self.builder.UpdateEdge(e, c2, face, PREC)
        self.builder.Range(e, face, f, l)

    def loop_wire(self, loop_idx, reverse: bool, face: TopoDS_Face, surf=None, pm=None):
        """Wire for a loop; edges oriented so the loop runs CCW w.r.t. the
        natural surface normal when `reverse` is False."""
        lv = self.by[loop_idx].values
        fins = self.chain(lv["halfedge"], "forward")
        items = []
        for f in fins:
            fv = self.by[f].values
            e_idx = fv.get("edge", 0)
            if not e_idx:
                continue          # isolated-vertex loop
            e = self.edge(e_idx)
            csense = self._edge_sense[e_idx]
            fsense = _plus(fv.get("sense", 43))
            forward = (fsense == csense)
            if reverse:
                forward = not forward
            items.append((e, forward))
        if not items:
            return None
        if reverse:
            items.reverse()
        W = TopoDS_Wire()
        self.builder.MakeWire(W)
        from OCP.ShapeFix import ShapeFix_Edge
        sfe = ShapeFix_Edge()
        for e, fwd in items:
            if FIX_MODE >= 1:
                sfe.FixAddPCurve(e, face, False, PREC)
            self.builder.Add(W, TopoDS.Edge_s(e.Oriented(TopAbs_FORWARD if fwd else TopAbs_REVERSED)))
        if FIX_MODE == 3:
            from OCP.ShapeBuild import ShapeBuild_ReShape
            ctx = ShapeBuild_ReShape()
            sfw = ShapeFix_Wire(W, face, PREC)
            sfw.SetMaxTolerance(MAXTOL)
            sfw.SetContext(ctx)
            def rec():
                return sum(1 for e, _ in items if ctx.IsRecorded(e))
            trace = []
            for step in ("FixReorder", "FixSmall", "FixConnected", "FixEdgeCurves", "FixDegenerated", "FixSelfIntersection", "FixLacking", "FixClosed"):
                fn = getattr(sfw, step)
                try:
                    r = fn() if step not in ("FixSmall", "FixLacking", "FixClosed") else (fn(True) if step == "FixSmall" else fn())
                except TypeError:
                    r = fn(False)
                trace.append(f"{step}:{r}/{rec()}")
            print("TRACE", trace)
            return sfw.Wire()
        if FIX_MODE >= 2:
            sfw = ShapeFix_Wire(W, face, PREC)
            sfw.SetMaxTolerance(MAXTOL)
            sfw.Perform()
            return sfw.Wire()
        return W

    def face(self, f_idx, outward_is_back: bool) -> TopoDS_Face:
        fv = self.by[f_idx].values
        s_idx = fv.get("surface", 0)
        if not s_idx:
            raise Unsupported(f"face {f_idx} without surface")
        surf, pm = self.surface(s_idx)
        fsense = _plus(fv.get("sense", 43))
        ssense = _plus(self.by[s_idx].values.get("sense", 43))
        # Parasolid face normal vs Parasolid natural normal
        face_vs_natural = (fsense == ssense)
        # OCCT natural normal may be flipped relative to Parasolid's
        face_vs_occt = face_vs_natural != pm.flip
        F = TopoDS_Face()
        self.builder.MakeFace(F, surf, PREC)
        loops = self.chain(fv.get("loop", 0), "next")
        sname = self.by[s_idx].name
        for l in loops:
            for f in self.chain(self.by[l].values["halfedge"], "forward"):
                vidx = self.by[f].values.get("vertex", 0)
                if vidx:
                    pt = P(self.by[self.by[vidx].values["point"]].values["pvec"])
                    try:
                        self.rep.check("vertex_on_" + sname, pt.Distance(_foot(pt, surf)))
                    except Exception:
                        self.rep.check("vertex_on_" + sname, 1e9)
        for l in loops:
            W = self.loop_wire(l, reverse=not face_vs_occt, face=F, surf=surf, pm=pm)
            if W is not None:
                self.builder.Add(F, W)
        if FIX_MODE >= 2:
            sff = ShapeFix_Face(F)
            sff.SetPrecision(PREC)
            sff.SetMaxTolerance(MAXTOL)
            sff.Perform()
            F = sff.Face()
        # material side: outward normal == parasolid face normal for back faces
        out_vs_occt = face_vs_occt == outward_is_back
        return TopoDS.Face_s(F.Oriented(TopAbs_FORWARD if out_vs_occt else TopAbs_REVERSED))

    def body(self, b_idx) -> tuple[TopoDS_Shape, dict]:
        bv = self.by[b_idx].values
        btype = bv.get("body_type", 1)
        info = {"node": b_idx, "body_type": btype, "faces": 0, "face_errors": []}
        regions = self.chain(bv.get("region", 0), "next")
        shells_out = []
        face_cache: dict[int, TopoDS_Face] = {}
        for r in regions:
            rv = self.by[r].values
            solid_region = _ch(rv.get("type", 86)) == "S"
            if btype == 1 and not solid_region:
                continue
            for sh in self.chain(rv.get("shell", 0), "next"):
                sv = self.by[sh].values
                backs = self.chain(sv.get("face", 0), "next")
                fronts = self.chain(sv.get("front_face", 0), "next_front")
                S = TopoDS_Shell()
                self.builder.MakeShell(S)
                nf = 0
                for fidx, is_back in [(f, True) for f in backs] + [(f, False) for f in fronts]:
                    if btype != 1 and fidx in face_cache:
                        continue
                    try:
                        F = self.face(fidx, is_back)
                    except Exception as ex:
                        tb = traceback.extract_tb(ex.__traceback__)
                        where = ";".join(f"{Path(fr.filename).name}:{fr.lineno}" for fr in tb if "sld2step" in fr.filename)[-120:]
                        info["face_errors"].append(f"face {fidx}: {type(ex).__name__}: {str(ex)[:90]} @ {where}")
                        continue
                    face_cache[fidx] = F
                    self.builder.Add(S, F)
                    nf += 1
                if nf:
                    if btype != 1:
                        from OCP.ShapeFix import ShapeFix_Shell
                        fx = ShapeFix_Shell()
                        fx.Init(S)
                        fx.SetPrecision(PREC)
                        fx.FixFaceOrientation(S, True, False)
                        S = fx.Shell()
                    shells_out.append(S)
                    info["faces"] += nf
        if not shells_out:
            raise Unsupported("body produced no faces")
        if btype == 1:
            sol = TopoDS_Solid()
            self.builder.MakeSolid(sol)
            for S in shells_out:
                self.builder.Add(sol, S)
            shape = sol
            # orientation sanity
            cl = BRepClass3d_SolidClassifier(sol)
            cl.PerformInfinitePoint(PREC)
            if cl.State() == TopAbs_IN:
                shape = TopoDS.Solid_s(sol.Reversed())
                info["reversed"] = True
        else:
            if len(shells_out) == 1:
                shape = shells_out[0]
            else:
                comp = TopoDS_Compound(); self.builder.MakeCompound(comp)
                for S in shells_out:
                    self.builder.Add(comp, S)
                shape = comp
        return shape, info


# --------------------------------------------------------------------- driver
_QUADRICS_V = ("Geom_CylindricalSurface", "Geom_ConicalSurface")


def _ref_radius(s) -> float:
    return s.Radius() if s.DynamicType().Name() == "Geom_CylindricalSurface" else s.RefRadius()


def _v_window(s, pts, other_r: float):
    """``s`` trimmed in v to the chart's v range, padded by the range plus four
    times both radii (an intersection loop of two quadrics stays within that)."""
    vs = []
    for p in pts:
        pr = GeomAPI_ProjectPointOnSurf(p, s)
        if pr.NbPoints() == 0:
            return None
        vs.append(pr.LowerDistanceParameters()[1])
    a, b = min(vs), max(vs)
    pad = (b - a) + 4 * (_ref_radius(s) + other_r) + 1.0
    return Geom_RectangularTrimmedSurface(s, a - pad, b + pad, False)


def shape_stats(shape) -> dict:
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    vol = props.Mass()
    sp = GProp_GProps()
    BRepGProp.SurfaceProperties_s(shape, sp)
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box, False)
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    ana = BRepCheck_Analyzer(shape)
    n = {"faces": 0, "edges": 0, "solids": 0}
    for kind, key in ((TopAbs_FACE, "faces"), (TopAbs_EDGE, "edges"), (TopAbs_SOLID, "solids")):
        ex = TopExp_Explorer(shape, kind)
        while ex.More():
            n[key] += 1; ex.Next()
    return {"valid": bool(ana.IsValid()), "volume_mm3": vol, "area_mm2": sp.Mass(),
            "bbox_mm": [xmin, ymin, zmin, xmax, ymax, zmax], **n}


def heal(shape):
    """One shape-wide ShapeFix pass: a single replacement context keeps edges
    that get split (seams, degeneracies) shared between neighbouring faces."""
    sfs = ShapeFix_Shape(shape)
    sfs.SetPrecision(PREC)
    sfs.SetMaxTolerance(MAXTOL)
    sfs.Perform()
    return sfs.Shape()


def closed_sheets_to_solids(shape):
    """A SolidWorks surface body whose shells are closed is a solid for our purposes."""
    shells = []
    ex = TopExp_Explorer(shape, TopAbs_SHELL)
    while ex.More():
        shells.append(TopoDS.Shell_s(ex.Current())); ex.Next()
    if not shells or not all(BRep_Tool.IsClosed_s(sh) for sh in shells):
        return shape
    out = []
    for sh in shells:
        sol = ShapeFix_Solid().SolidFromShell(sh)
        if sol is None or sol.IsNull():
            return shape
        out.append(sol)
    if len(out) == 1:
        return out[0]
    comp = TopoDS_Compound(); b = BRep_Builder(); b.MakeCompound(comp)
    for s_ in out:
        b.Add(comp, s_)
    return comp


def ensure_solid(shape):
    """Turn whatever ShapeFix returned (solid, shell, compound of shells) into
    solids; returns a solid or a compound of solids."""
    if shape.ShapeType() == TopAbs_SOLID:
        return shape
    solids = []
    ex = TopExp_Explorer(shape, TopAbs_SOLID)
    while ex.More():
        solids.append(ex.Current()); ex.Next()
    if not solids:
        ex = TopExp_Explorer(shape, TopAbs_SHELL)
        while ex.More():
            sh = TopoDS.Shell_s(ex.Current())
            sol = ShapeFix_Solid().SolidFromShell(sh)
            if sol is not None and not sol.IsNull():
                solids.append(sol)
            ex.Next()
    if not solids:
        return shape
    if len(solids) == 1:
        return solids[0]
    comp = TopoDS_Compound(); b = BRep_Builder(); b.MakeCompound(comp)
    for s in solids:
        b.Add(comp, s)
    return comp


def convert_nodes(nodes, name="part") -> tuple[TopoDS_Shape, Report]:
    shapes, rep = convert_bodies(nodes, name)
    if len(shapes) == 1:
        return shapes[0], rep
    comp = TopoDS_Compound(); BRep_Builder().MakeCompound(comp)
    for s in shapes:
        BRep_Builder().Add(comp, s)
    return comp, rep


def convert_bodies(nodes, name="part") -> tuple[list, Report]:
    """One healed shape per Parasolid body."""
    rep = Report(name)
    conv = Converter(nodes, rep)
    bodies = [n.index for n in nodes if n.name == "BODY"]
    shapes = []
    for b in bodies:
        try:
            shp, info = conv.body(b)
        except Exception as ex:
            rep.errors.append(f"body {b}: {ex}")
            continue
        healed = heal(shp)
        if info["body_type"] == 1:
            healed = ensure_solid(healed)
        elif info["body_type"] == 3:
            healed = closed_sheets_to_solids(healed)
        st = shape_stats(healed)
        if st["solids"] and st["volume_mm3"] < 0:
            healed = healed.Reversed()
            st = shape_stats(healed)
            info["reversed"] = True
        if not st["valid"]:
            st0 = shape_stats(shp)
            if st0["valid"]:
                healed, st = shp, st0
        shp = healed
        info.update(st)
        rep.bodies.append(info)
        shapes.append(shp)
    if not shapes:
        raise RuntimeError("no body converted: " + "; ".join(rep.errors))
    return shapes, rep


def write_step(shape, out: Path, name: str):
    Interface_Static.SetCVal_s("write.step.schema", "AP214")
    Interface_Static.SetCVal_s("write.step.unit", "MM")
    Interface_Static.SetCVal_s("write.step.product.name", name)
    w = STEPControl_Writer()
    if w.Transfer(shape, STEPControl_AsIs) != IFSelect_RetDone:
        raise RuntimeError("STEP transfer failed")
    out.parent.mkdir(parents=True, exist_ok=True)
    if w.Write(str(out)) != IFSelect_RetDone:
        raise RuntimeError("STEP write failed")


def write_glb(bodies: list, out: Path, name: str, budget_mb: float | None = None):
    """Single part as .glb: a root node ``name`` (kind part) with one child node per body."""
    import sld2glb
    from OCP.TDocStd import TDocStd_Document
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.XCAFApp import XCAFApp_Application
    from OCP.XCAFDoc import XCAFDoc_DocumentTool
    from OCP.TDataStd import TDataStd_Name
    from OCP.TopLoc import TopLoc_Location
    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))
    app.NewDocument(TCollection_ExtendedString("MDTV-XCAF"), doc)
    st = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    comp = sld2glb.component_name(name)
    root = st.NewShape()
    TDataStd_Name.Set_s(root, TCollection_ExtendedString(name))
    meta = {sld2glb.label_entry(root): {"kind": "part", "component": comp, "file": name, "_xcaf_name": name}}
    for i, b in enumerate(bodies, 1):
        sld2glb.mesh_shape(b)
        lab = st.AddShape(b, False)
        TDataStd_Name.Set_s(lab, TCollection_ExtendedString(name))
        c = st.AddComponent(root, lab, TopLoc_Location())
        meta[sld2glb.label_entry(c)] = {"kind": "body", "component": comp, "body": i, "_xcaf_name": f"{name} body {i}"}
    st.UpdateAssemblies()
    return sld2glb.write_glb_from_doc(doc, st, out, meta, {"source": name, "bodies": len(bodies)},
                                      budget_mb if budget_mb is not None else sld2glb.BUDGET_MB)


def convert_file(src: Path, out: Path | None, glb: Path | None = None, budget_mb: float | None = None) -> Report:
    sys.path.insert(0, str(Path(__file__).parent))
    from sldload import load_transmits
    t0 = time.time()
    transmits = load_transmits(src)
    if not transmits:
        raise RuntimeError("no Parasolid body in file")
    reps = []
    shapes = []
    bodies = []
    for stream, t, nodes in transmits:
        try:
            bs, rep = convert_bodies(nodes, src.stem)
        except Exception as ex:
            r = Report(src.stem); r.errors.append(f"{stream}: {ex}")
            reps.append(r); continue
        bodies += bs
        if len(bs) == 1:
            shp = bs[0]
        else:
            shp = TopoDS_Compound(); BRep_Builder().MakeCompound(shp)
            for b in bs:
                BRep_Builder().Add(shp, b)
        shapes.append(shp); reps.append(rep)
    if not shapes:
        raise RuntimeError("no body converted: " + "; ".join(e for r in reps for e in r.errors)[:300])
    if len(shapes) == 1:
        shape = shapes[0]
    else:
        comp = TopoDS_Compound(); BRep_Builder().MakeCompound(comp)
        for s in shapes:
            BRep_Builder().Add(comp, s)
        shape = comp
    if out is not None:
        write_step(shape, out, src.stem)
    glb_rep = write_glb(bodies, glb, src.stem, budget_mb) if glb is not None else None
    rep = Report(src.stem)
    for r in reps:
        rep.bodies += r.bodies; rep.warnings += [w for w in r.warnings if w not in rep.warnings]; rep.errors += r.errors
        for k, c in r.geom_checks.items():
            g = rep.geom_checks.setdefault(k, {"n": 0, "bad": 0, "max": 0.0})
            g["n"] += c["n"]; g["bad"] += c["bad"]; g["max"] = max(g["max"], c["max"])
    rep.seconds = time.time() - t0
    rep.glb = glb_rep
    return rep


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="SLDPRT -> STEP (offline, open source)")
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("-o", "--out", default=".")
    ap.add_argument("--json", action="store_true", help="print a JSON report per file")
    ap.add_argument("--glb", action="store_true",
                    help="also write <out>/<name>.glb (one node per body, meshopt-compressed) for web viewers")
    ap.add_argument("--no-step", action="store_true", help="skip the STEP file (with --glb)")
    ap.add_argument("--budget-mb", type=float, default=None,
                    help="simplify the heaviest meshes when the .glb is larger (default 30)")
    a = ap.parse_args(argv)
    if a.no_step and not a.glb:
        ap.error("--no-step needs --glb")
    outdir = Path(a.out)
    rc = 0
    for inp in a.inputs:
        p = Path(inp)
        files = sorted(p.rglob("*.SLDPRT")) + sorted(p.rglob("*.sldprt")) if p.is_dir() else [p]
        for f in files:
            out = outdir / (f.stem + ".step")
            try:
                rep = convert_file(f, None if a.no_step else out,
                                   outdir / (f.stem + ".glb") if a.glb else None, a.budget_mb)
                nb = len(rep.bodies); nv = sum(1 for b in rep.bodies if b["valid"])
                vol = sum(b["volume_mm3"] for b in rep.bodies)
                flag = "OK " if nv == nb and not rep.errors else "WARN"
                bad = {k: c for k, c in rep.geom_checks.items() if c["bad"]}
                print(f"{flag} {f.name}: {nb} bodies ({nv} valid), {vol:.1f} mm3, {rep.seconds:.1f}s"
                      + (f"  warnings={rep.warnings}" if rep.warnings else "")
                      + (f"  errors={rep.errors}" if rep.errors else "")
                      + (f"  GEOM={bad}" if bad else ""), flush=True)
                for line in (rep.glb.lines() if rep.glb else []):
                    print("    " + line)
                if a.json:
                    print(json.dumps({k: v for k, v in rep.__dict__.items() if k != "glb"}, indent=1, default=str))
            except Exception as ex:
                rc = 1
                print(f"FAIL {f.name}: {ex}")
                traceback.print_exc()
    return rc


if __name__ == "__main__":
    sys.exit(main())
