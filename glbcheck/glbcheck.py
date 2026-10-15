"""Check a .glb written by sldasm2step/sld2step --glb against its sources.

  .venv/bin/python glbcheck/glbcheck.py out/x.glb --asm x.SLDASM [--exclude GLOB ...] [--step out/x.step]

* loads the .glb in three.js r180 (GLTFLoader + MeshoptDecoder, node) and lists its named nodes;
* walks the assembly's COMPINSTANCETREE independently of the builder and compares
  the placed part / sub-assembly instances (component name -> count);
* reads the STEP in FreeCAD and compares the bounding boxes (mm).
"""
from __future__ import annotations
import argparse, collections, fnmatch, json, os, subprocess, sys, tempfile
from pathlib import Path, PureWindowsPath

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
import sldasm2step as sa
import sld2glb

FC_BBOX = r'''
import os, json, Part
sh = Part.read(os.environ["STEPFILE"])
bb = sh.optimalBoundingBox() if hasattr(sh, "optimalBoundingBox") else sh.BoundBox
open(os.environ["JSONOUT"], "w").write(json.dumps([bb.XMin, bb.YMin, bb.ZMin, bb.XMax, bb.YMax, bb.ZMax]))
'''


def expected(asm: Path, exclude, roots, gen_fasteners=False):
    """Placed part / sub-assembly counts by component name, from the SolidWorks
    tree (configuration-aware read_tree, each placement expanded)."""
    import fasteners
    res = sa.Resolver(roots or [asm.parent])
    gen = fasteners.Generator() if gen_fasteners else None
    parts, asms = collections.Counter(), collections.Counter()
    def walk(path, config, mult):
        for r in sa.read_tree(path, config):
            if r.suppressed:
                continue
            stem = PureWindowsPath(r.path).stem
            if gen is not None and r.doctype == "PART" and fasteners.family(stem.upper()):
                sc = gen.screw(stem.upper(), r.config, res.resolve(r.path, r.doctype))
                if sc is not None:
                    parts[sc.name] += mult
                continue
            if any(fnmatch.fnmatchcase(stem.upper(), x.upper()) for x in exclude):
                continue
            t = res.resolve(r.path, r.doctype)
            if t is None:
                continue
            name = sld2glb.component_name(stem)
            if r.doctype == "ASSEMBLY":
                asms[name] += mult
                walk(t, r.config or None, mult)          # recursion counts every placement
            else:
                parts[name] += mult
    walk(asm, None, 1)
    return parts, asms


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("glb")
    ap.add_argument("--asm")
    ap.add_argument("--exclude", action="append", default=[])
    ap.add_argument("--root", action="append")
    ap.add_argument("--step")
    ap.add_argument("--fasteners", action="store_true", help="the .glb was made with --fasteners")
    a = ap.parse_args()
    s = json.loads(subprocess.run(["node", str(HERE / "check.mjs"), a.glb], check=True,
                                  capture_output=True, text=True).stdout)
    kinds = collections.Counter(n.get("kind") for n in s["named"])
    orphans = [n for n in s["named"] if "orphanMesh" in n]
    ids = [n["id"] for n in s["named"] if "id" in n]
    print(f"three.js r180: parsed; {kinds['part']} part, {kinds['body']} body, {kinds['assembly']} assembly nodes, "
          f"{s['meshes']} mesh objects, {s['uniqueGeometries']} unique geometries, "
          f"{s['uniqueTris']} unique / {s['placedTris']} placed triangles, {len(orphans)} meshes without a part node, "
          f"ids unique: {len(ids) == len(set(ids))}")
    ok = not orphans and len(ids) == len(set(ids))
    if a.asm:
        parts, asms = expected(Path(a.asm), a.exclude, [Path(r) for r in a.root] if a.root else None,
                               a.fasteners)
        got_p = collections.Counter(n["component"] for n in s["named"] if n.get("kind") == "part")
        got_a = collections.Counter(n["component"] for n in s["named"] if n.get("kind") == "assembly")
        got_a[sld2glb.component_name(Path(a.asm).stem)] -= 1       # the root node
        got_a += collections.Counter()
        failed = s["sceneExtras"].get("failed", [])
        dp = {k: (parts[k], got_p[k]) for k in parts.keys() | got_p.keys() if parts[k] != got_p[k]}
        da = {k: (asms[k], got_a[k]) for k in asms.keys() | got_a.keys() if asms[k] != got_a[k]}
        print(f"instances: expected {sum(parts.values())} parts / {sum(asms.values())} sub-assemblies, "
              f"glb {sum(got_p.values())} / {sum(got_a.values())}; mismatches (expected, glb): parts {dp or 'none'}, "
              f"assemblies {da or 'none'}" + (f"; failed parts reported: {len(failed)}" if failed else ""))
        ok &= not dp and not da
    print("glb bbox mm :", s["bbox_mm"])
    if a.step:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "bb.json"; scr = Path(td) / "bb.py"
            scr.write_text(FC_BBOX)
            env = dict(os.environ, STEPFILE=a.step, JSONOUT=str(out))
            subprocess.run(["freecadcmd", "-c", f"exec(open({str(scr)!r}).read())"], env=env,
                           capture_output=True, check=True)
            bb = json.loads(out.read_text())
        # glb is metres in the SolidWorks frame; STEP is mm in the same frame
        dev = max(abs(x - y) for x, y in zip(bb, s["bbox_mm"]))
        print("step bbox mm:", [round(x, 3) for x in bb], f"max deviation {dev:.3f} mm")
        ok &= dev < 1.0
    print("CHECK", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
