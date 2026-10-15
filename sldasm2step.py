"""SolidWorks .SLDASM -> STEP assembly (offline, open source).

Component placement comes from the assembly's own ``swXmlContents/COMPINSTANCETREE``
XML: every ``swReference`` carries a 4x4 row-major transform (translation in the
last row, metres) and points at a part or sub-assembly file.  Parts are converted
with :mod:`sld2step`; sub-assemblies recurse.  Output is an XCAF document written
as STEP AP214 with the component names, so the assembly tree survives.
"""
from __future__ import annotations

import collections
import fnmatch
import glob
import hashlib
import json
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

sys.path.insert(0, str(Path(__file__).parent))

from OCP.gp import gp_Trsf
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS_Compound, TopoDS_Shape
from OCP.BRep import BRep_Builder
from OCP.TDocStd import TDocStd_Document
from OCP.TCollection import TCollection_ExtendedString, TCollection_AsciiString
from OCP.XCAFDoc import XCAFDoc_DocumentTool
from OCP.TDataStd import TDataStd_Name
from OCP.STEPCAFControl import STEPCAFControl_Writer
from OCP.STEPControl import STEPControl_AsIs
from OCP.Interface import Interface_Static
from OCP.IFSelect import IFSelect_RetDone
from OCP.XCAFApp import XCAFApp_Application
from OCP.BRepTools import BRepTools

from sldprt2xt.container import open_container
import sld2step
import sld2glb
import fasteners
from sldload import load_all, select_config, stored_config_ids

NS = "{http://www.solidworks.com/sw2003/schema}"
SCALE = 1000.0


@dataclass
class Ref:
    name: str
    path: str            # Windows path as stored by SolidWorks
    doctype: str         # PART / ASSEMBLY
    config: str
    suppressed: bool
    hidden: bool
    virtual: bool
    T: list              # 16 doubles, row-major 4x4, translation in row 4 (metres)


@dataclass
class AsmReport:
    file: str
    parts_converted: int = 0
    parts_failed: list = field(default_factory=list)
    unresolved: list = field(default_factory=list)
    suppressed: int = 0
    excluded: list = field(default_factory=list)
    generated: list = field(default_factory=list)        # "<name> [<cfg>]" per instance
    config_mismatch: list = field(default_factory=list)  # "<stem> wants [..] stored [..]"
    instances: int = 0
    seconds: float = 0.0
    glb: object = None        # sld2glb.GlbReport when --glb


def read_tree(path: Path, config: str | None = None) -> list[Ref]:
    """Direct components of ``path`` in assembly configuration ``config``
    (default: the active one).

    The XML holds one ``swModel`` per (document, configuration) and each owns
    its direct ``swReference`` children: the assembly's own configurations
    (e.g. RETRACTED / EXTENDED, each listing every component again) and the
    contents of its sub-assemblies (in the sub-assembly's frame).  Only the
    chosen configuration's own references are components here; sub-assemblies
    are read from their own files by the caller.
    """
    c = open_container(path)
    blob = next((b for b in c.blobs if b.name == "swXmlContents/COMPINSTANCETREE"), None)
    if blob is None:
        raise RuntimeError(f"{path.name}: no COMPINSTANCETREE stream")
    root = ET.fromstring(blob.data.decode("utf-8"))
    files = {f.get("id"): f for f in root.iter(NS + "swFile")}
    models = {m.get("id"): m for m in root.iter(NS + "swModel")}
    cfgs = list(root.iter(NS + "swConfiguration"))
    # (an Element without children is falsy: no `or` between the lookups)
    pick = next((x for x in cfgs if config and x.get("swName") == config), None)
    if pick is None:
        pick = next((x for x in cfgs if x.get("swMostRecentConfiguration") == "YES"), None)
    top = models.get(pick.get("swModelRef")) if pick is not None else None
    scope = top.findall(NS + "swReference") if top is not None else root.iter(NS + "swReference")
    refs = []
    for r in scope:
        m = models.get(r.get("swModelRef"))
        f = files.get(m.get("swFileRef")) if m is not None else None
        if f is None:
            continue
        refs.append(Ref(
            name=r.get("swName") or r.get("swComponentName") or "",
            path=f.get("swPath"),
            doctype=f.get("swDocType"),
            config=r.get("swConfigurationName", ""),
            suppressed=r.get("swSuppressed") == "YES",
            hidden=r.get("swHidden") == "YES",
            virtual=r.get("swIsVirtualComponent") == "YES",
            T=[float(x) for x in r.get("swTransform").split()],
        ))
    return refs


class Resolver:
    """Map the Windows paths stored in the assembly to files of the Pack and Go."""

    def __init__(self, roots: list[Path]):
        self.files: dict[str, Path] = {}
        for root in roots:
            # case-insensitive: vendor files in a Pack and Go are often .sldprt
            for f in glob.glob(str(root / "**" / "*.[Ss][Ll][Dd]*"), recursive=True):
                p = Path(f)
                if p.suffix.upper() in (".SLDPRT", ".SLDASM"):
                    self.files.setdefault(p.stem.upper(), p)
        self.suffixes = sorted({k.rsplit("_", 1)[1] for k in self.files if "_" in k and k.rsplit("_", 1)[1].isdigit()})

    def resolve(self, winpath: str, doctype: str) -> Path | None:
        n = PureWindowsPath(winpath).stem.upper()
        ext = ".SLDPRT" if doctype == "PART" else ".SLDASM"
        for cand in [n + "_" + sfx for sfx in self.suffixes] + [n]:
            p = self.files.get(cand)
            if p is not None and p.suffix.upper() == ext:
                return p
        for k, p in self.files.items():
            if k.startswith(n + "_") and p.suffix.upper() == ext:
                return p
        return None


def trsf_from_sw(T: list) -> gp_Trsf:
    """SolidWorks row-vector convention p' = p.R + t  ->  OCCT column form."""
    R = [T[0:3], T[4:7], T[8:11]]
    t = [x * SCALE for x in T[12:15]]
    tr = gp_Trsf()
    tr.SetValues(R[0][0], R[1][0], R[2][0], t[0],
                 R[0][1], R[1][1], R[2][1], t[1],
                 R[0][2], R[1][2], R[2][2], t[2])
    return tr


class AssemblyBuilder:
    def __init__(self, resolver: Resolver, report: AsmReport, include_hidden=True,
                 exclude: tuple = (), gen_fasteners: bool = False, cache: Path | None = None):
        self.resolver = resolver
        self.gen = fasteners.Generator() if gen_fasteners else None
        self._gen_labels = {}       # Screw -> label
        self._gen_names = {}        # label entry -> generated screw name
        self._loaded = {}           # path -> (PartConfigs, transmits)
        self.cache = cache          # folder of converted parts (.brep), keyed by file + stored configuration
        self.meta = {}              # XCAF label entry -> glTF node name/extras (sld2glb)
        self.exclude = tuple(x.upper() for x in exclude)
        self.rep = report
        self.include_hidden = include_hidden
        self.app = XCAFApp_Application.GetApplication_s()
        self.doc = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))
        self.app.NewDocument(TCollection_ExtendedString("MDTV-XCAF"), self.doc)
        self.st = XCAFDoc_DocumentTool.ShapeTool_s(self.doc.Main())
        self._part_labels = {}      # path -> label (or None when failed)
        self._asm_labels = {}       # path -> label

    # ---- parts
    def part_label(self, path: Path, config: str = ""):
        """Label of the part's stored geometry for ``config``.  A part stores
        one configuration's bodies (sometimes two, e.g. a flat pattern); an
        instance asking for another one is recorded in ``config_mismatch``."""
        key = str(path)
        try:
            if key not in self._loaded:
                self._loaded[key] = load_all(path)
            configs, every = self._loaded[key]
            transmits = select_config(every, configs, config)
        except Exception as ex:
            self.rep.parts_failed.append(f"{path.name}: {ex}")
            self._loaded[key] = (None, [])
            return None
        ids = stored_config_ids(transmits)
        stored = [configs.names.get(i, f"id {i}") for i in ids]
        if config and configs.names and config not in stored:
            self.rep.config_mismatch.append(
                f"{path.stem} wants [{config}] stored [{'|'.join(stored) or '?'}]")
        key = (key, tuple(ids))
        if key in self._part_labels:
            return self._part_labels[key]
        label = None
        try:
            shape = self._cached(path, ids)
            if shape is None:
                failed = []
                shapes = []
                sld2step.progress(f"Converting part {len(self._part_labels) + 1}: {path.name}")
                for stream, t, nodes in transmits:
                    try:
                        shp, rep = sld2step.convert_nodes(nodes, path.stem)
                    except Exception as ex:
                        failed.append(f"{path.name} [{stream}]: {ex}")
                        continue
                    shapes.append(shp)
                self.rep.parts_failed += failed
                if not shapes:
                    raise RuntimeError("no body converted")
                if len(shapes) == 1:
                    shape = shapes[0]
                else:
                    shape = TopoDS_Compound(); b = BRep_Builder(); b.MakeCompound(shape)
                    for s_ in shapes:
                        b.Add(shape, s_)
                self._store(path, ids, shape, failed)
            label = self.st.AddShape(shape, False)
            TDataStd_Name.Set_s(label, TCollection_ExtendedString(path.stem))
            self.rep.parts_converted += 1
        except Exception as ex:
            self.rep.parts_failed.append(f"{path.name}: {ex}")
            label = None
        self._part_labels[key] = label
        return label

    CACHE_VERSION = "2"     # bump when conversion output changes

    def _cache_key(self, path: Path, ids):
        if self.cache is None:
            return None
        st = path.stat()
        h = hashlib.sha1(f"{self.CACHE_VERSION}|{path.resolve()}|{st.st_size}|{st.st_mtime_ns}|{','.join(ids)}"
                         .encode()).hexdigest()[:16]
        return self.cache / f"{path.stem}-{h}"

    def _cached(self, path: Path, ids):
        k = self._cache_key(path, ids)
        if k is None or not k.with_suffix(".brep").exists():
            return None
        shape = TopoDS_Shape()
        if not BRepTools.Read_s(shape, str(k.with_suffix(".brep")), BRep_Builder()):
            return None
        self.rep.parts_failed += json.loads(k.with_suffix(".json").read_text())["failed"]
        return shape

    def _store(self, path: Path, ids, shape, failed: list):
        k = self._cache_key(path, ids)
        if k is None:
            return
        k.parent.mkdir(parents=True, exist_ok=True)
        k.with_suffix(".json").write_text(json.dumps({"file": path.name, "configs": list(ids), "failed": failed}))
        BRepTools.Write_s(shape, str(k.with_suffix(".brep")))

    def fastener_label(self, ref: Ref, stem: str):
        """--fasteners: a known fastener family (fasteners.py) is generated from
        its configuration name instead of converted.  Returns (label, handled):
        handled with no label = unparseable configuration, excluded."""
        if self.gen is None or fasteners.family(stem) is None:
            return None, False
        target = self.resolver.resolve(ref.path, ref.doctype)
        sc = self.gen.screw(stem, ref.config, target)
        if sc is None:
            self.rep.excluded.append(f"{ref.name} [{ref.config}] (fastener size not recognised)")
            return None, True
        if sc not in self._gen_labels:
            label = self.st.AddShape(self.gen.shape(sc), False)
            TDataStd_Name.Set_s(label, TCollection_ExtendedString(sc.name))
            self._gen_labels[sc] = label
            self._gen_names[sld2glb.label_entry(label)] = sc.name
        self.rep.generated.append(f"{sc.name} [{ref.config}]")
        return self._gen_labels[sc], True

    # ---- assemblies
    def assembly_label(self, path: Path, config: str | None = None, root: bool = True):
        key = (str(path), config)
        if key in self._asm_labels:
            return self._asm_labels[key]
        label = self.st.NewShape()
        TDataStd_Name.Set_s(label, TCollection_ExtendedString(path.stem))
        if root:
            self.meta[sld2glb.label_entry(label)] = {
                "kind": "assembly", "component": sld2glb.component_name(path.stem),
                "file": path.name, "_xcaf_name": path.stem}
        self._asm_labels[key] = label
        for ref in read_tree(path, config):
            if ref.suppressed:
                self.rep.suppressed += 1
                continue
            if ref.hidden and not self.include_hidden:
                continue
            stem = PureWindowsPath(ref.path).stem.upper()
            gen_label, handled = self.fastener_label(ref, stem) if ref.doctype == "PART" else (None, False)
            if handled:
                if gen_label is not None:
                    comp = self.st.AddComponent(label, gen_label, TopLoc_Location(trsf_from_sw(ref.T)))
                    TDataStd_Name.Set_s(comp, TCollection_ExtendedString(ref.name))
                    self.meta[sld2glb.label_entry(comp)] = {
                        "kind": "part", "component": self._gen_names[sld2glb.label_entry(gen_label)],
                        "config": ref.config, "generated": True,
                        "file": PureWindowsPath(ref.path).name, "_xcaf_name": ref.name}
                    self.rep.instances += 1
                continue
            if any(fnmatch.fnmatchcase(stem, x) for x in self.exclude):
                # left out on purpose (e.g. a configurable screw whose stored
                # geometry is another size -- ISSUES.md #1), and reported
                self.rep.excluded.append(f"{ref.name} [{ref.config}]")
                continue
            target = self.resolver.resolve(ref.path, ref.doctype)
            if target is None:
                self.rep.unresolved.append(f"{path.stem}: {ref.name} ({PureWindowsPath(ref.path).name})")
                continue
            child = (self.assembly_label(target, ref.config or None, root=False) if ref.doctype == "ASSEMBLY"
                     else self.part_label(target, ref.config))
            if child is None:
                continue
            comp = self.st.AddComponent(label, child, TopLoc_Location(trsf_from_sw(ref.T)))
            TDataStd_Name.Set_s(comp, TCollection_ExtendedString(ref.name))
            self.meta[sld2glb.label_entry(comp)] = {
                "kind": "assembly" if ref.doctype == "ASSEMBLY" else "part",
                "component": sld2glb.component_name(PureWindowsPath(ref.path).stem),
                "config": ref.config, "file": target.name, "_xcaf_name": ref.name}
            self.rep.instances += 1
        return label

    def write(self, out: Path):
        self.st.UpdateAssemblies()
        Interface_Static.SetCVal_s("write.step.schema", "AP214")
        Interface_Static.SetCVal_s("write.step.unit", "MM")
        w = STEPCAFControl_Writer()
        w.SetNameMode(True)
        if not w.Transfer(self.doc, STEPControl_AsIs):
            raise RuntimeError("STEP transfer failed")
        out.parent.mkdir(parents=True, exist_ok=True)
        if w.Write(str(out)) != IFSelect_RetDone:
            raise RuntimeError("STEP write failed")

    def write_glb(self, out: Path, budget_mb: float = sld2glb.BUDGET_MB, scene_extras=None):
        self.st.UpdateAssemblies()
        for label in [l for l in self._part_labels.values() if l is not None] + list(self._gen_labels.values()):
            sld2glb.mesh_shape(self.st.GetShape_s(label))
        return sld2glb.write_glb_from_doc(self.doc, self.st, out, self.meta, scene_extras, budget_mb)


def convert_assembly(src: Path, out: Path | None, roots: list[Path] | None = None,
                     exclude: tuple = (), gen_fasteners: bool = False, glb: Path | None = None,
                     cache: Path | None = None, budget_mb: float = sld2glb.BUDGET_MB) -> AsmReport:
    """Write the assembly as STEP to ``out`` and/or as .glb to ``glb``."""
    t0 = time.time()
    rep = AsmReport(src.name)
    resolver = Resolver(roots or [src.parent])
    b = AssemblyBuilder(resolver, rep, exclude=exclude, gen_fasteners=gen_fasteners, cache=cache)
    sld2step.progress(f"Reading the assembly tree of {src.name}")
    b.assembly_label(src)
    if out is not None:
        sld2step.progress(f"Writing the STEP file for {src.name}")
        b.write(out)
    if glb is not None:
        sld2step.progress(f"Building the preview of {src.name}")
        rep.glb = b.write_glb(glb, budget_mb, scene_extras={
            "source": src.name,
            "generated": dict(sorted(collections.Counter(rep.generated).items())),
            "config_mismatch": dict(sorted(collections.Counter(rep.config_mismatch).items())),
            "excluded": dict(sorted(collections.Counter(rep.excluded).items())),
            "unresolved": sorted(set(rep.unresolved)), "failed": rep.parts_failed})
    rep.seconds = time.time() - t0
    return rep


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="SLDASM -> STEP assembly (offline, open source)")
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("-o", "--out", default=".")
    ap.add_argument("--root", action="append", help="folder(s) to resolve component files in (default: the assembly's folder)")
    ap.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                    help="leave out components whose file stem matches (case-insensitive), "
                         "e.g. 'BHCS*'; repeatable, listed in the report")
    ap.add_argument("--fasteners", action="store_true",
                    help="generate standard cap screws (SHCS ISO 4762, BHCS ISO 7380-1; see "
                         "fasteners.py) from each instance's configuration name instead of "
                         "converting the part's one stored size; takes precedence over --exclude")
    ap.add_argument("--glb", action="store_true",
                    help="also write <out>/<name>.glb: instanced, meshopt-compressed glTF for web viewers")
    ap.add_argument("--no-step", action="store_true", help="skip the STEP file (with --glb)")
    ap.add_argument("--budget-mb", type=float, default=sld2glb.BUDGET_MB,
                    help="simplify the heaviest meshes when the .glb is larger (default %(default)s)")
    ap.add_argument("--cache", metavar="DIR", help="keep converted parts (.brep) here and reuse them")
    a = ap.parse_args(argv)
    if a.no_step and not a.glb:
        ap.error("--no-step needs --glb")
    rc = 0
    for inp in a.inputs:
        src = Path(inp)
        out = Path(a.out) / (src.stem + ".step")
        glb = Path(a.out) / (src.stem + ".glb")
        try:
            rep = convert_assembly(src, None if a.no_step else out,
                                   [Path(r) for r in a.root] if a.root else None,
                                   tuple(a.exclude), a.fasteners, glb if a.glb else None,
                                   Path(a.cache) if a.cache else None, a.budget_mb)
            print(f"OK  {src.name}: {rep.instances} instances, {rep.parts_converted} parts converted, "
                  f"{len(rep.parts_failed)} failed, {len(rep.unresolved)} unresolved, {rep.suppressed} suppressed, "
                  f"{len(rep.excluded)} excluded, {len(rep.generated)} generated, {rep.seconds:.1f}s")
            for x, n in sorted(collections.Counter(rep.generated).items()):
                print(f"    generated: {x} x{n}")
            for x, n in sorted(collections.Counter(rep.config_mismatch).items()):
                print(f"    config-mismatch: {x} x{n}")
            for x, n in sorted(collections.Counter(rep.excluded).items()):
                print(f"    excluded: {x}" + (f" x{n}" if n > 1 else ""))
            for u in sorted(set(rep.unresolved))[:20]:
                print("    unresolved:", u)
            for f in rep.parts_failed:
                print("    failed:", f)
            for line in (rep.glb.lines() if rep.glb else []):
                print("    " + line)
        except Exception as ex:
            rc = 1
            import traceback; traceback.print_exc()
            print(f"FAIL {src.name}: {ex}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
