"""XCAF document -> compact, instanced, compressed glTF binary (.glb) for web viewers.

Used by ``sldasm2step.py --glb`` (assemblies) and ``sld2step.py --glb`` (parts).

1. Every unique part shape is tessellated once (``BRepMesh_IncrementalMesh``,
   0.5 mm / 0.35 rad) and written with Open CASCADE's ``RWGltf_CafWriter``: each
   placement becomes a node that references the shared mesh data.
2. The writer's node names are placeholders (the XCAF label entry); they are
   replaced by the component names and ``extras`` below, duplicate mesh objects
   are merged, and one neutral material is added.
3. ``gltfpack`` (meshoptimizer, pinned in package.json) quantizes and
   meshopt-compresses the file.  If it is still over budget, the heaviest meshes
   are simplified with gltfpack's simplifier and the report says so.

Output contract (see README "glTF output"): coordinates are the SolidWorks
model's (Y up), in metres.  Named nodes carry ``extras.kind`` =
``assembly`` / ``part`` / ``body``; each part/body node holds its mesh on one
unnamed child node (gltfpack's dequantization transform).
"""
from __future__ import annotations

import collections
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.BRep import BRep_Tool
from OCP.TopExp import TopExp_Explorer
from OCP.TopAbs import TopAbs_FACE
from OCP.TopoDS import TopoDS
from OCP.TopLoc import TopLoc_Location
from OCP.TCollection import TCollection_AsciiString, TCollection_ExtendedString
from OCP.TColStd import TColStd_IndexedDataMapOfStringString
from OCP.TDataStd import TDataStd_Name
from OCP.TDF import TDF_Tool
from OCP.Message import Message_ProgressRange
from OCP.RWGltf import RWGltf_CafWriter
from OCP.RWMesh import (RWMesh_NameFormat_Instance, RWMesh_NameFormat_Product,
                        RWMesh_CoordinateSystem_glTF)

HERE = Path(__file__).parent
LIN_DEFL = 0.5       # mm, absolute chordal deviation
ANG_DEFL = 0.35      # rad
BUDGET_MB = 30.0     # packed .glb size above which the heaviest meshes get simplified
POS_BITS = 16        # gltfpack position quantization; scene-wide grid (2 m scene -> 0.03 mm)
PACK_SUFFIX = re.compile(r"_\d{6}$")    # Pack and Go rename: NAME_123456
MATERIAL = {"name": "default",
            "pbrMetallicRoughness": {"baseColorFactor": [0.72, 0.74, 0.77, 1.0],
                                     "metallicFactor": 0.2, "roughnessFactor": 0.6}}


def component_name(stem: str) -> str:
    """Part/assembly file stem without the Pack-and-Go ``_NNNNNN`` suffix."""
    return PACK_SUFFIX.sub("", stem)


def label_entry(label) -> str:
    s = TCollection_AsciiString()
    TDF_Tool.Entry_s(label, s)
    return s.ToCString()


def mesh_shape(shape, lin: float = LIN_DEFL, ang: float = ANG_DEFL) -> int:
    """Tessellate in place (stored on the faces, shared by every placement); return triangles."""
    BRepMesh_IncrementalMesh(shape, lin, False, ang, True)
    n = 0
    ex = TopExp_Explorer(shape, TopAbs_FACE)
    while ex.More():
        tri = BRep_Tool.Triangulation_s(TopoDS.Face_s(ex.Current()), TopLoc_Location())
        if tri is not None:
            n += tri.NbTriangles()
        ex.Next()
    return n


# ---------------------------------------------------------------- glb i/o
def read_glb(path: Path) -> tuple[dict, bytes]:
    b = Path(path).read_bytes()
    magic, ver, total = struct.unpack("<III", b[:12])
    if magic != 0x46546C67:
        raise ValueError(f"{path}: not a glb")
    off, j, bin_ = 12, None, b""
    while off < total:
        ln, typ = struct.unpack("<II", b[off:off + 8])
        chunk = b[off + 8:off + 8 + ln]
        if typ == 0x4E4F534A:
            j = json.loads(chunk)
        elif typ == 0x004E4942:
            bin_ = chunk
        off += 8 + ln
    return j, bin_


def write_glb(path: Path, j: dict, bin_: bytes):
    bin_ = bytes(bin_) + b"\0" * (-len(bin_) % 4)
    if j.get("buffers"):
        j["buffers"][0]["byteLength"] = len(bin_)
    js = json.dumps(j, separators=(",", ":"), ensure_ascii=False).encode()
    js += b" " * (-len(js) % 4)
    total = 12 + 8 + len(js) + (8 + len(bin_) if bin_ else 0)
    with open(path, "wb") as f:
        f.write(struct.pack("<III", 0x46546C67, 2, total))
        f.write(struct.pack("<II", len(js), 0x4E4F534A)); f.write(js)
        if bin_:
            f.write(struct.pack("<II", len(bin_), 0x004E4942)); f.write(bin_)


def mesh_triangles(j: dict, mi: int) -> int:
    n = 0
    for p in j["meshes"][mi]["primitives"]:
        if "indices" in p:
            n += j["accessors"][p["indices"]]["count"] // 3
        else:
            n += j["accessors"][p["attributes"]["POSITION"]]["count"] // 3
    return n


def glb_stats(j: dict) -> dict:
    """Named nodes by kind, unique meshes, unique and placed triangles."""
    uses = collections.Counter(n["mesh"] for n in j.get("nodes", []) if "mesh" in n)
    kinds = collections.Counter((n.get("extras") or {}).get("kind") for n in j.get("nodes", []))
    tri = {m: mesh_triangles(j, m) for m in range(len(j.get("meshes", [])))}
    return {"parts": kinds.get("part", 0), "bodies": kinds.get("body", 0),
            "assemblies": kinds.get("assembly", 0), "meshes": len(tri),
            "triangles": sum(tri.values()),
            "placed_triangles": sum(tri[m] * c for m, c in uses.items())}


# ---------------------------------------------------------------- stage 1: OCCT writer
def write_raw(doc, shape_tool, out: Path, meta: dict[str, dict], roots: list):
    """Write ``doc`` with RWGltf_CafWriter and rename its nodes from ``meta``.

    ``meta`` maps an XCAF label entry (component labels and the root labels) to
    the node's name and extras.  Their XCAF names are swapped for the entry while
    writing, then restored, so a STEP written from the same document is unchanged.
    """
    from OCP.TDF import TDF_Label
    saved = []
    for entry, m in meta.items():
        lab = TDF_Label()
        TDF_Tool.Label_s(doc.GetData(), TCollection_AsciiString(entry), lab, False)
        if lab.IsNull():
            raise RuntimeError(f"no XCAF label {entry}")
        saved.append((lab, m.get("_xcaf_name")))
        TDataStd_Name.Set_s(lab, TCollection_ExtendedString("@" + entry))
    try:
        w = RWGltf_CafWriter(TCollection_AsciiString(str(out)), True)
        w.SetMergeFaces(True)                       # one primitive per part
        w.SetParallel(True)
        w.SetNodeNameFormat(RWMesh_NameFormat_Instance)
        w.SetMeshNameFormat(RWMesh_NameFormat_Product)
        cs = w.ChangeCoordinateSystemConverter()
        cs.SetInputLengthUnit(0.001)                # mm -> m
        cs.SetInputCoordinateSystem(RWMesh_CoordinateSystem_glTF)   # SolidWorks is Y-up: no rotation
        if not w.Perform(doc, TColStd_IndexedDataMapOfStringString(), Message_ProgressRange()):
            raise RuntimeError("glTF writer failed")
    finally:
        for lab, name in saved:
            if name:
                TDataStd_Name.Set_s(lab, TCollection_ExtendedString(name))
    j, bin_ = read_glb(out)
    _rename(j, meta)
    _dedupe_meshes(j)
    j.setdefault("materials", []).append(MATERIAL)
    mat = len(j["materials"]) - 1
    for m in j.get("meshes", []):
        for p in m["primitives"]:
            p.setdefault("material", mat)
    write_glb(out, j, bin_)
    return j


def _rename(j: dict, meta: dict):
    nodes = j["nodes"]
    for n in nodes:
        nm = n.get("name", "")
        if nm.startswith("@") and nm[1:] in meta:
            m = meta[nm[1:]]
            n["name"] = m["component"]
            n["extras"] = {k: v for k, v in m.items() if not k.startswith("_")}
        else:
            raise RuntimeError(f"glTF node without metadata: {nm!r}")
    # stable per-instance id: path of component#k (k counts same-named siblings, 1-based)
    def walk(i, prefix):
        seen = collections.Counter()
        for c in nodes[i].get("children", []):
            cn = nodes[c]["name"]
            seen[cn] += 1
            cid = f"{prefix}/{cn}#{seen[cn]}" if prefix else f"{cn}#{seen[cn]}"
            nodes[c]["extras"]["id"] = cid
            walk(c, cid)
    for s in j.get("scenes", []):
        for r in s.get("nodes", []):
            nodes[r]["extras"]["id"] = nodes[r]["name"]
            walk(r, "")
    for m in j.get("meshes", []):
        if "name" in m:
            m["name"] = component_name(m["name"])


def _dedupe_meshes(j: dict):
    """The OCCT writer shares accessors between placements but repeats the mesh object."""
    key_to_new, remap, meshes = {}, {}, []
    for i, m in enumerate(j.get("meshes", [])):
        k = json.dumps(m["primitives"], sort_keys=True)
        if k not in key_to_new:
            key_to_new[k] = len(meshes); meshes.append(m)
        remap[i] = key_to_new[k]
    j["meshes"] = meshes
    for n in j["nodes"]:
        if "mesh" in n:
            n["mesh"] = remap[n["mesh"]]


# ---------------------------------------------------------------- stage 2: gltfpack
def gltfpack_bin() -> list[str]:
    env = os.environ.get("GLTFPACK")
    if env:
        return [env]
    local = HERE / "node_modules" / ".bin" / "gltfpack"
    if local.exists():
        return [str(local)]
    raise RuntimeError("gltfpack not found: run `npm ci` in the sld2step folder (see README)")


def gltfpack(src: Path, dst: Path, extra: list[str] = ()):
    cmd = gltfpack_bin() + ["-i", str(src), "-o", str(dst), *extra]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not dst.exists():
        raise RuntimeError(f"gltfpack failed ({r.returncode}): {r.stderr.strip()[-400:]}")


PACK_ARGS = ["-cc", "-ce", "ext", "-kn", "-ke", "-vp", str(POS_BITS)]


# ---------------------------------------------------------------- simplification of single meshes
def _accessor_bytes(j, bin_, ai) -> bytes:
    a = j["accessors"][ai]; bv = j["bufferViews"][a["bufferView"]]
    size = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}[a["type"]] * {5120: 1, 5121: 1, 5122: 2, 5123: 2, 5125: 4, 5126: 4}[a["componentType"]]
    stride = bv.get("byteStride", size)
    start = bv.get("byteOffset", 0) + a.get("byteOffset", 0)
    if stride == size:
        return bin_[start:start + size * a["count"]]
    return b"".join(bin_[start + i * stride:start + i * stride + size] for i in range(a["count"]))


def _one_mesh_glb(j, bin_, mi, path: Path):
    """Standalone glb holding mesh ``mi`` only (float positions/normals, uint32 indices)."""
    buf = bytearray(); views = []; accs = []
    prims = []
    for p in j["meshes"][mi]["primitives"]:
        attrs = {}
        for name, ai in list(p["attributes"].items()) + [("_IDX", p.get("indices"))]:
            if ai is None:
                continue
            a = j["accessors"][ai]
            data = _accessor_bytes(j, bin_, ai)
            if name == "_IDX" and a["componentType"] != 5125:
                fmt = "<%d%s" % (a["count"], "H" if a["componentType"] == 5123 else "B")
                data = struct.pack("<%dI" % a["count"], *struct.unpack(fmt, data))
            buf += b"\0" * (-len(buf) % 4)
            views.append({"buffer": 0, "byteOffset": len(buf), "byteLength": len(data)})
            buf += data
            na = {k: v for k, v in a.items() if k not in ("bufferView", "byteOffset")}
            na["bufferView"] = len(views) - 1
            if name == "_IDX":
                na["componentType"] = 5125
            accs.append(na)
            attrs[name] = len(accs) - 1
        idx = attrs.pop("_IDX", None)
        prims.append({"attributes": attrs, "mode": 4, **({"indices": idx} if idx is not None else {})})
    mj = {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}],
          "nodes": [{"name": "m", "mesh": 0}], "meshes": [{"primitives": prims}],
          "accessors": accs, "bufferViews": views, "buffers": [{"byteLength": len(buf)}]}
    write_glb(path, mj, bytes(buf))


def simplify_meshes(raw: Path, out: Path, targets: dict[int, float], tmp: Path) -> dict[int, tuple[int, int]]:
    """Replace each mesh in ``targets`` (mesh index -> triangle ratio) by gltfpack's
    simplification of it; write the result (still uncompressed) to ``out``."""
    j, bin_ = read_glb(raw)
    bin_ = bytearray(bin_)
    done = {}
    for mi, ratio in targets.items():
        before = mesh_triangles(j, mi)
        a, b = tmp / f"m{mi}.glb", tmp / f"m{mi}s.glb"
        _one_mesh_glb(j, bytes(bin_), mi, a)
        gltfpack(a, b, ["-noq", "-si", f"{ratio:.4f}", "-kn"])
        sj, sbin = read_glb(b)
        sm = next(n["mesh"] for n in sj["nodes"] if "mesh" in n)
        new_prims = []
        for p in sj["meshes"][sm]["primitives"]:
            np_ = {"mode": 4, "attributes": {}}
            for name, ai in list(p["attributes"].items()) + [("_IDX", p.get("indices"))]:
                if ai is None:
                    continue
                data = _accessor_bytes(sj, sbin, ai)
                bin_ += b"\0" * (-len(bin_) % 4)
                j["bufferViews"].append({"buffer": 0, "byteOffset": len(bin_), "byteLength": len(data)})
                bin_ += data
                na = {k: v for k, v in sj["accessors"][ai].items() if k not in ("bufferView", "byteOffset")}
                na["bufferView"] = len(j["bufferViews"]) - 1
                j["accessors"].append(na)
                if name == "_IDX":
                    np_["indices"] = len(j["accessors"]) - 1
                else:
                    np_["attributes"][name] = len(j["accessors"]) - 1
            np_["material"] = j["meshes"][mi]["primitives"][0].get("material", 0)
            new_prims.append(np_)
        j["meshes"][mi]["primitives"] = new_prims
        done[mi] = (before, mesh_triangles(j, mi))
    write_glb(out, j, bytes(bin_))   # orphaned old accessors are dropped by the final gltfpack
    return done


# ---------------------------------------------------------------- driver
@dataclass
class GlbReport:
    path: str = ""
    raw_bytes: int = 0
    bytes: int = 0
    stats: dict = field(default_factory=dict)
    simplified: dict = field(default_factory=dict)   # mesh name -> (before, after)
    over_budget: bool = False

    def lines(self) -> list[str]:
        s = self.stats
        out = [f"glb: {self.path} {self.bytes / 1e6:.1f} MB (unpacked {self.raw_bytes / 1e6:.1f} MB), "
               + (f"{s['bodies']} body nodes, " if s.get("bodies") else f"{s.get('parts', 0)} part nodes, ")
               + f"{s.get('meshes', 0)} unique meshes, "
               f"{s.get('triangles', 0)} triangles ({s.get('placed_triangles', 0)} placed)"]
        if self.simplified:
            b = sum(x[0] for x in self.simplified.values()); a = sum(x[1] for x in self.simplified.values())
            out.append(f"simplified: {b} -> {a} triangles in {len(self.simplified)} meshes ("
                       + ", ".join(f"{k} {x[0]}->{x[1]}" for k, x in self.simplified.items()) + ")")
        if self.over_budget:
            out.append(f"glb over budget: {self.bytes / 1e6:.1f} MB")
        return out


def write_glb_from_doc(doc, shape_tool, out: Path, meta: dict, scene_extras: dict | None = None,
                       budget_mb: float = BUDGET_MB, keep_raw: bool = False) -> GlbReport:
    rep = GlbReport(path=str(out))
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".glb-", dir=out.parent))
    try:
        raw = tmp / "raw.glb"
        j = write_raw(doc, shape_tool, raw, meta, [])
        rep.raw_bytes = raw.stat().st_size
        gltfpack(raw, out, PACK_ARGS)
        size = out.stat().st_size
        budget = budget_mb * 1e6
        src = raw
        for attempt in range(4):
            if size <= budget:
                break
            j, _ = read_glb(src)
            tri = {m: mesh_triangles(j, m) for m in range(len(j["meshes"]))}
            total = sum(tri.values())
            # triangles to remove, assuming packed size scales with unique triangles
            need = total * (1 - 0.9 * budget / size)
            # heaviest meshes first, until they hold at least twice what must go
            heavy, acc = [], 0
            for m in sorted(tri, key=tri.get, reverse=True):
                heavy.append(m); acc += tri[m]
                if acc >= 2 * need:
                    break
            ratio = max(0.05, 1 - need / acc)
            nxt = tmp / f"simp{attempt}.glb"
            done = simplify_meshes(src, nxt, {m: ratio for m in heavy}, tmp)
            for m, (b, a) in done.items():
                name = j["meshes"][m].get("name", f"mesh{m}")
                b0 = rep.simplified.get(name, (b, a))[0]
                rep.simplified[name] = (b0, a)
            src = nxt
            gltfpack(src, out, PACK_ARGS)
            size = out.stat().st_size
        rep.over_budget = size > budget
        rep.bytes = size
        rep.stats = glb_stats(read_glb(out)[0])
        ex = dict(scene_extras or {})
        ex["tessellation"] = {"linear_mm": LIN_DEFL, "angular_rad": ANG_DEFL}
        ex["simplified"] = {k: list(v) for k, v in rep.simplified.items()}
        jj, bb = read_glb(out)
        jj["scenes"][0]["extras"] = ex
        write_glb(out, jj, bb)
        rep.bytes = out.stat().st_size
        if keep_raw:
            shutil.copy(raw, out.with_suffix(".raw.glb"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return rep
