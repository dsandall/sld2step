"""Browser entry point: runs the sld2step CLIs inside Pyodide (driven by worker.js).

The converter modules are the same files as the command-line tool.  Two things
differ in the browser: the sldprt2xt patch is applied to the installed package
here instead of by ``setup.sh``, and ``gltfpack`` (a subprocess) is replaced by a
plain copy, so the preview ``.glb`` stays unpacked.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path, PureWindowsPath

APP = Path("/app")


def _apply_patch(patch: str, root: Path):
    """Apply a unified diff to files under ``root`` (exact-context hunks only)."""
    target, hunks, cur = None, [], None
    for line in patch.splitlines():
        if line.startswith("+++ "):
            target = root / line[4:].split("\t")[0].strip()
        elif line.startswith("@@"):
            cur = ([], []); hunks.append((target, cur))
        elif cur is not None and line[:1] in (" ", "-", "+"):
            if line[0] in " -":
                cur[0].append(line[1:])
            if line[0] in " +":
                cur[1].append(line[1:])
        elif cur is not None and line == "":
            cur[0].append(""); cur[1].append("")
    for path, (old, new) in hunks:
        src = path.read_text()
        a, b = "\n".join(old), "\n".join(new)
        if b in src:
            continue                      # already applied
        if src.count(a) != 1:
            raise RuntimeError(f"patch does not apply to {path.name}")
        path.write_text(src.replace(a, b))


def _ocp_compat():
    """OCP.wasm binds the TopoDS casts as functions of a ``TopoDS`` namespace
    (``TopoDS.Edge``); the native wheels bind them as static methods (``Edge_s``)."""
    from OCP.TopoDS import TopoDS
    for name in ("Vertex", "Edge", "Wire", "Face", "Shell", "Solid", "Compound"):
        if not hasattr(TopoDS, name + "_s") and hasattr(TopoDS, name):
            setattr(TopoDS, name + "_s", getattr(TopoDS, name))


def setup():
    sys.path.insert(0, str(APP))
    _ocp_compat()
    import sldprt2xt
    site = Path(sldprt2xt.__file__).parent.parent
    for p in sorted((APP / "patches").glob("*.patch")):
        _apply_patch(p.read_text(), site)
    import sld2glb
    sld2glb.gltfpack = lambda src, dst, extra=(): shutil.copyfile(src, dst)
    import sld2step, sldasm2step  # noqa: F401  (import cost paid at load, not at first drop)


def _top_assemblies(asms: list[Path]) -> list[Path]:
    """Assemblies no other given assembly places (Pack-and-Go suffixes allowed)."""
    import sldasm2step
    used = set()
    for a in asms:
        try:
            refs = sldasm2step.read_tree(a)
        except Exception:
            continue
        used |= {PureWindowsPath(r.path).stem.upper() for r in refs if r.doctype == "ASSEMBLY"}
    def placed(p):
        s = p.stem.upper()
        return any(s == u or s.startswith(u + "_") for u in used)
    return [a for a in asms if not placed(a)] or asms


def convert(inp: str, out: str, fasteners: bool = False) -> int:
    """Convert everything under ``inp`` into ``out``; report lines go to stdout."""
    import sld2step, sldasm2step
    shutil.rmtree(out, ignore_errors=True)
    Path(out).mkdir(parents=True)
    files = sorted(p for p in Path(inp).rglob("*") if p.is_file())
    asms = [p for p in files if p.suffix.upper() == ".SLDASM"]
    parts = [p for p in files if p.suffix.upper() == ".SLDPRT"]
    if asms:
        args = [str(a) for a in _top_assemblies(asms)]
        args += ["-o", out, "--root", inp, "--glb", "--budget-mb", "1e9"]
        return sldasm2step.main(args + (["--fasteners"] if fasteners else []))
    if parts:
        return sld2step.main([str(p) for p in parts] + ["-o", out, "--glb", "--budget-mb", "1e9"])
    print("FAIL no .SLDPRT or .SLDASM file among the dropped files")
    return 1
