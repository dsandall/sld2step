# sld2step — SolidWorks → STEP, offline, open source

Converts SolidWorks `.SLDPRT` parts and `.SLDASM` assemblies to STEP AP214 on
Linux with no SolidWorks license, no cloud service and no commercial CAD reader.

```
./setup.sh                                   # venv + deps + vendored patch, ~1 min
.venv/bin/python sld2step.py  part.SLDPRT   -o out/     # one part
.venv/bin/python sld2step.py  "CAD folder/" -o out/     # every .SLDPRT below it
.venv/bin/python sldasm2step.py top.SLDASM  -o out/     # assembly with sub-assemblies
.venv/bin/python sldasm2step.py top.SLDASM  -o out/ --fasteners   # + generate sized cap screws
.venv/bin/python sldasm2step.py top.SLDASM  -o out/ --fasteners --glb   # + out/top.glb for a web viewer
.venv/bin/python sld2step.py  part.SLDPRT   -o out/ --glb --no-step     # part as .glb only
STEPDIR=out freecadcmd -c "exec(open('fccheck.py').read())"      # independent validation (FreeCAD)
STEPFILE=out/x.step freecadcmd -c "exec(open('fccheck_one.py').read())"  # one file per process (robust)
```

Each part prints one line: `OK`/`WARN`, body count, how many bodies are valid
closed solids, total volume, time, and any geometry warnings. `--json` adds a
full report (per-body validity, volume, bounding box, dropped faces, checks).

## Pipeline

1. **Container + Parasolid decode — `sldprt2xt`** (PyPI, AGPL-3.0). Opens both
   SolidWorks container generations, inflates the streams, decodes the embedded
   Parasolid transmit into typed nodes using the full base schema 13006.
2. **Transmit selection — `sldload.py`.** Native parts keep their bodies in
   `Contents/Config-N-Partition`. Imported (vendor) parts keep an empty partition
   and the bodies live as separate transmits under
   `Config-N-FeatureBodies/LocalBodies`; those are taken instead.
3. **B-rep rebuild — `sld2step.py`.** Every Parasolid node type in the corpus is
   mapped onto Open CASCADE (via the `cadquery-ocp-novtk` wheel): planes,
   cylinders, cones, spheres, tori (incl. lemon tori), NURBS surfaces, swept and
   spun surfaces, offset surfaces; lines, circles, ellipses, NURBS curves,
   trimmed curves, surface-parameter (SP) curves, intersection curves. Topology
   is rebuilt edge-shared from Parasolid's fins/loops/shells with the documented
   sense conventions, then one shape-wide `ShapeFix_Shape` pass adds seams,
   degenerated edges and pcurves consistently. Solids are checked with
   `BRepCheck_Analyzer`; orientation is verified by volume sign.
4. **Assemblies — `sldasm2step.py`.** SolidWorks stores component placement in
   `swXmlContents/COMPINSTANCETREE` (an XML stream in the `.SLDASM`), not in the
   Parasolid data. Each `swReference` carries a 4×4 row-vector transform in
   metres, its configuration and suppressed/hidden flags, and points at the
   part or sub-assembly file. The builder resolves Pack-and-Go renames
   (`NAME_123456`), recurses into sub-assemblies, converts each part once, and
   writes an XCAF assembly STEP so component names survive.

## Configurations and generated fasteners

A part file stores geometry for one configuration only (the one active at
save; sheet metal also keeps its flat pattern, which is skipped). Config ids
are mapped to names (`sldload.read_configs`), and the assembly builder
reports every placed instance that asks for a configuration that is not
stored:

```
    config-mismatch: VENDOR_RAIL_123456 wants [SHORT] stored [LONG] x3
```

Such an instance is still placed with the stored shape. Configured standard
cap screws (one file, hundreds of sizes) are the common case; with
`--fasteners`, components whose file stem is `SHCS*` or `BHCS*` are not
converted but generated from the instance's configuration name
(`M6x1.0|10mm|18-8 SS|FULL`, `M6×1|6mm|18-8|FULL`) by `fasteners.py`:

```
    generated: SHCS M6x1.0x10 [M6x1.0|10mm|18-8 SS|FULL] x4
    generated: SHCS M4x0.7x40 PT [M4x0.7|40mm|18-8 SS|PARTIAL] x16
```

- **Name** (STEP product): `<SHCS|BHCS> M<d>x<pitch>x<length>`, ` PT` appended
  for a partial thread. Instance names stay as in the assembly.
- **Tables:** SHCS ISO 4762 M1.6-M24; BHCS ISO 7380-1 M3-M16 plus M2/M2.5
  (manufacturer values, not in ISO 7380-1). Head diameter/height are the
  standard maxima, hex key the nominal size.
- **Shape:** thread = plain cylinder at nominal diameter, no helix; partial
  thread length from the part's `ThreadLength` property when present, else
  ISO 4762 b (the shank face is split there); hex socket 0.75 k deep; no
  chamfers or fillets. Same frame as the stored bodies (axis +Y, bearing face
  y = 0). Generating the stored size reproduces the stored body (SHCS
  identical, BHCS within 0.004 % volume).
- Every generated screw is always generated, also when the stored size is
  the one asked for, so all sizes are named and reported the same way.
- Sizes outside the tables and unparseable or empty names are left out and
  listed as `excluded:`. `--fasteners` takes precedence over `--exclude`.
- Output lines are these: `generated:` = standard-size screw made from its
  size, not the customer's CAD; `config-mismatch:` = placed, wrong shape
  likely; `excluded:` = left out.

## glTF output (`--glb`) for web viewers

`--glb` writes `<out>/<file stem>.glb` next to the STEP (`--no-step` skips the
STEP). Needs `npm ci` in this folder once (`setup.sh` does it): it installs
`gltfpack` 1.2.0 (pinned in `package.json`), plus three.js 0.180.0 and esbuild
for the check harness only.

How it is made (`sld2glb.py`):

1. Each unique part (or generated screw) is tessellated **once**
   (`BRepMesh_IncrementalMesh`, 0.5 mm chordal, 0.35 rad) and the XCAF document
   the assembly builder already has is written by Open CASCADE's
   `RWGltf_CafWriter`: every placement is a node referencing the shared mesh.
2. Node names/extras are set as below, one neutral material is added.
3. `gltfpack -cc -ce ext -kn -ke -vp 16`: `EXT_meshopt_compression` +
   `KHR_mesh_quantization`, 16-bit positions on one scene-wide grid (0.03 mm on a
   2 m assembly). `-kn`/`-ke` keep every named node and its extras; `-mi`/`-mm`
   are not used, so instances are never merged.
4. If the packed file is over `--budget-mb` (default 30), the heaviest meshes are
   simplified with gltfpack's simplifier (one mesh at a time, `-si`, 1 % error
   limit) and the report prints `simplified: <before> -> <after> triangles ...`.
   None of the test assemblies needed it.

Load it in three.js with `GLTFLoader` + `loader.setMeshoptDecoder(MeshoptDecoder)`
(`three/examples/jsm/libs/meshopt_decoder.module.js`).

### Naming and structure contract

- Units and axes: **metres, SolidWorks model frame** (Y up, as glTF); no rotation
  is applied. The STEP is the same frame in millimetres.
- Every *named* node has `extras` (three.js: `object.userData`) with
  `kind`, `component` and `id`:

| `kind` | node name = `component` | other extras | children |
|---|---|---|---|
| `assembly` | assembly PN, file stem without the Pack-and-Go `_NNNNNN` (e.g. `RACK-ASM`) | `file`; `config` (not on the root) | its placed components, with their local transforms |
| `part` | part file stem without `_NNNNNN` (e.g. `BRACKET-01`); a generated screw is named like `SHCS M6x1.0x10` | `config` (the instance's configuration), `file`; `generated: true` for `--fasteners` screws | exactly one unnamed node holding the mesh |
| `body` | (single-part `.glb` only) the part name | `body`: 1-based index | exactly one unnamed node holding the mesh |

- The scene has one root node: the top assembly (`kind: assembly`), or for a part
  `.glb` the part (`kind: part`) with one `body` child per body.
- `id` is unique in the file and stable while the SolidWorks tree is unchanged:
  the slash-joined path of `component#k` from the root's children down, `k`
  counting same-named siblings in file order, e.g.
  `RACK-ASM#1/BRACKET-01#3`.
- The mesh node under a `part`/`body` node carries gltfpack's dequantization
  transform; move/hide/highlight the named node, not the mesh node. All
  instances of a part share one `BufferGeometry`.
- three.js renames duplicate node names (`X`, `X_1`, ...) and strips `.`/`:`/`/`
  and spaces, so use `userData.name` (the original glTF name) or
  `userData.component`. Grouping/counting: collect nodes with
  `userData.kind === 'part'` by `userData.component`. Picking: from the hit mesh
  walk `parent` up to the first `kind` `part`/`body` (an instance) and on to
  `kind` `assembly` (a sub-assembly).
- `gltf.scene.userData` (scene extras): `source`, `generated`,
  `config_mismatch`, `excluded` (label -> count), `unresolved`, `failed`,
  `tessellation`, `simplified` (mesh -> [before, after]).

Stdout keeps the STEP report lines (`OK ...`, `generated:`, `config-mismatch:`,
`excluded:`, `unresolved:`, `failed:`) and adds `glb: <path> <MB> ...` and, only
when it happened, `simplified: ...` or `glb over budget: ...`.
`--cache DIR` keeps converted parts as `.brep` for re-runs.

Check (`glbcheck/`): `glbcheck.py x.glb --asm x.SLDASM [--fasteners] --step x.step`
parses the file with three r180 in node, compares the placed parts and
sub-assemblies per component with an independent walk of the SolidWorks tree,
and the bounding box with FreeCAD's reading of the STEP; `shot.sh x.glb x.png`
renders it in headless Chrome.

On the private test package, six assemblies from 19 to 484 placed parts
came out at 0.07 to 6.7 MB (3.5 to 8x smaller than unpacked; the largest has
79 unique meshes, 2.0 M placed triangles). No simplification was needed.
Instance counts match the tree, and bounding boxes match the STEP to 0.01 mm,
except where one bad edge in a vendor part widens FreeCAD's STEP box to
±128 m (ISSUES.md #2); the meshes are unaffected.

## Exact vs. approximated

| Geometry | Handling |
|---|---|
| Analytic surfaces and curves | exact |
| NURBS curves/surfaces, SP-curves | exact (same knots, poles, parametrisation) |
| Swept / spun / offset surfaces | exact (OCCT extrusion / revolution / offset) |
| Intersection curves | exact surface/surface intersection in OCCT; the Parasolid chart picks the branch and direction; falls back to a C2 interpolation through the chart when no branch matches |
| Rolling-ball and cliff-edge blends | **approximated**: circular sections sampled along the spine, contact points taken from the blend face's own boundary rails, fitted with a B-spline surface. Fine for quoting, volumes and viewing; not for downstream Boolean work on the fillet itself |
| Foreign (PE) geometry, helices | not seen in the corpus, unsupported |

Units: Parasolid works in metres; output is millimetres.

## Validation

Every output is re-read by FreeCAD (a separate OCCT build) with
`fccheck.py`, which reports validity, closedness, solid count, volume and
bounding box. Development used one private customer Pack-and-Go (about 150
parts, a dozen assemblies), which is not included:

| | result |
|---|---|
| Parts | 0 failures; 131 of 150 files with every body a valid closed solid; ~1400 bodies, ~1250 valid solids; worst case 4 min (a 26-body, 13 000-face part) |
| Customer-designed parts | **all** valid closed solids in FreeCAD |
| Vendor parts | all 19 files with an invalid body: approximated blends, vanishing fillets, or bodies already broken in the SolidWorks file |
| FreeCAD re-read | 121 of 138 unique parts valid, 116 valid closed solids, 60 pass FreeCAD's strict Boolean check |
| Largest assembly | 769 placed instances, 135 part shapes, 292 generated cap screws, 10 min, 516 MB STEP; references to virtual or missing components skipped and listed |

Generated screws were checked in four assemblies (63 screws): head against a
face of the part it fastens, shank coaxial with a hole of matching diameter
(clearance or tap drill), no head/material overlap. Every table size passes
FreeCAD's strict Boolean check.

A public sample set is still to come; until then these numbers cannot be
reproduced outside the original package.

Strict Boolean checks fail mostly on pcurve/3D-curve tolerance mismatches
after healing; the solids are valid and closed, and volumes agree with the
Parasolid data. Tighten `PREC`/`MAXTOL` in `sld2step.py` if a downstream
Boolean workflow needs it.

Two self-checks run inside the converter and show up in the report when they
fail: every loop vertex must lie on its rebuilt surface, and every edge end
must lie on its rebuilt curve. These catch convention errors immediately
(they are how the cone-axis sign in the 2006 XT reference was found to be
wrong).

## Known limitations

- Only the configuration that was resolved when the file was saved is
  exported; other configurations exist only as properties in the file. In an
  assembly an instance of another configuration is reported as
  `config-mismatch:` but still gets the stored shape; standard cap screws can
  be generated instead (`--fasteners`), other parts can be left out with
  `--exclude GLOB` -- see `ISSUES.md` #1.
- Blend surfaces are approximations (see table). A handful of vendor parts
  with vanishing blends still report one invalid body.
- Assembly components that are virtual (stored inside the assembly) or
  missing from the package are skipped and listed as unresolved.
- No colours, materials or PMI.

## Licensing

AGPL-3.0-only (`LICENSE`), the licence of `sldprt2xt`, which this tool
imports and patches. Reading SolidWorks files you were given, and
reverse engineering a file format for interoperability, is lawful use; keep
customer CAD off cloud converters whose terms grant rights over uploads.
