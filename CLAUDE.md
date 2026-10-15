# CLAUDE.md — notes for future agents

SolidWorks `.SLDPRT` / `.SLDASM` → STEP, fully offline, open-source stack. No
SolidWorks, no cloud, no commercial reader. Public, AGPL-3.0-only.

**Confidentiality.** It was developed on a confidential customer CAD package.
Never commit customer files, renders, part numbers, file or folder names,
assembly names, or per-part measurements from real customer data, in code,
docs or commit messages. Describe findings generically ("a vendor part",
"a 769-part assembly").

## Read first
- `README.md` — setup, usage, what is exact vs approximated, validation results.
- `sld2step.py` module docstring — the Parasolid conventions the converter relies on.

## How it works (one paragraph)
`sldprt2xt` (PyPI, AGPL) opens the SolidWorks container and decodes the embedded
Parasolid transmit into typed nodes with the full base schema 13006. `sldload.py`
picks the right transmit (Partition for native parts, `FeatureBodies/LocalBodies`
for imported/vendor parts). `sld2step.py` rebuilds every face in Open CASCADE
(`cadquery-ocp-novtk` wheel) from the Parasolid topology and geometry, heals it
with one shape-wide ShapeFix pass, and writes STEP AP214. `sldasm2step.py` reads
the assembly's `swXmlContents/COMPINSTANCETREE` XML (component transforms live
there, not in Parasolid) and writes an XCAF assembly STEP with names.

## Hard-won facts (do not rediscover)
- The 2006 Parasolid XT reference has the **cone formula axis sign inverted**:
  real data is `R = P + vA + (X cos u + Y sin u)(r + v tan a)`.
- Newlines inside an `.x_t` are 80-column wrapping, not separators.
- Shape-wide `ShapeFix_Shape` is mandatory; per-face `ShapeFix_Face` copies ring
  edges it splits at seams and the shell falls apart ("NotConnected").
- The vertex order of an edge is authoritative; when the sense flags disagree
  with the projected parameters on an open curve, trust geometry.
- Rolling-ball blends are approximated (sampled sections along the spine, contact
  points from the blend face's own rails). Everything else is exact.
- `sldprt2xt` 1.2.0 sizes `legal_owners` wrong for some SolidWorks 2025 files;
  `patches/` carries the fix, `setup.sh` applies it.
- FreeCAD headless: pass paths via env vars. A positional directory argument is
  opened as a document and hangs.

- `.glb` (`sld2glb.py`): `RWGltf_CafWriter` shares accessors between placements
  but repeats the mesh object (deduped afterwards); gltfpack moves each mesh onto
  an unnamed child node (dequantization transform) and quantizes on one
  scene-wide grid; three.js `GLTFLoader` uniquifies node names, the original is
  in `userData.name`. The naming contract is in README "glTF output".
- `GeomAPI_IntSS` on untrimmed cylinder x cone takes seconds per call; bound
  both surfaces in v around the chart first (`_v_window`).

## Commit convention
`KIND: scope: summary` (see the `commit-naming` skill). This repo's two variables:

- **Domain kind: `CONV`** — changes to the geometry conversion itself: Parasolid
  node → OCCT geometry/topology, healing, orientation, STEP writing.
- **Scopes:** `sldprt` (part converter), `sldasm` (assembly builder), `load`
  (container/transmit selection), `check` (validation harness, FreeCAD), `patches`
  (vendored fixes to dependencies), `web` (browser version in `web/`), `docs`.

## Testing
Use only the public test set: `samples/fetch.sh` (MIT files, pinned commits,
SHA-256 checked; the CAD files stay untracked). The browser version must give the
same report lines as the CLI on it (`web/build.sh`, `python3 web/serve.py`).
