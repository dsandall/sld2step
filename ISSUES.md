# Known issues

## 1. Configured parts come out in the configuration last saved, not the one used

**What happens.** A SolidWorks part file stores Parasolid geometry for **one**
configuration only: the one active when the file was saved
(`Contents/Config-<id>-Partition`). Every other configuration exists only as
properties (`docProps/Config-<id>-Properties.xml`) and a feature recipe; there
is no second partition, and no tessellated copy either (`Contents/DisplayLists`
is the saved configuration's too; the assembly does not cache its components'
display lists).

`sldasm2step.py` reads each instance's configuration (`Ref.config`) but can
only convert the part's stored body, so an instance that asks for another
configuration gets the stored shape.

**Where it bites.** Generic fastener parts are one file with dozens of size
configurations: an assembly asks for `M6×1|6mm|18-8|FULL`, `M4x0.7|20mm|...`
and so on, and every instance gets the one size that was saved. Vendor parts
with length variants (LONG/SHORT, "FULL LENGTH") and configured nuts behave
the same way.

**Status.**

*Fixed:*
- **Detection, any part.** `sldload.read_configs` maps config ids to names
  from `swXmlContents/Features` (`Features__ZLB`, zlib after a 24-byte
  header, in older files): each `swModel` carries `swConfigurationId` (the
  `N` of `Config-N-*`) and `swConfigurationName`; `swConfigurationList`
  flags the active one. `CnfgObjs` and `3DExperienceExchange_ConfigHeader`
  were not needed. `sldasm2step.py` prints, per distinct case,
  `config-mismatch: <file stem> wants [<cfg>] stored [<cfg>] xN` for every
  placed instance whose configuration is not the stored one. The instance is
  still placed with the stored shape, but not silently.
- **Standard cap screws.** `sldasm2step.py --fasteners` generates SHCS
  (ISO 4762) and BHCS (ISO 7380-1) solids from the configuration name
  (`fasteners.py`) and prints `generated: <name> [<cfg>] xN`. Generating the
  stored configuration reproduces the converted stored body: SHCS identical
  (same volume and bounding box, Boolean symmetric difference 0), BHCS within
  0.004 % volume.
- **Sheet-metal flat patterns.** Sheet-metal parts store the flat pattern as a
  second partition (`DefaultSM-FLAT-PATTERN`); both bodies used to be exported
  on top of each other. Only the requested, else active, configuration's
  bodies are loaded now.
- **Assembly configurations.** `COMPINSTANCETREE` holds one component list per
  assembly configuration (and one per sub-assembly), and all of them used to
  be placed (a 769-part assembly came out with about 9500). Only the active
  list is placed now.

*Not fixed:*
- Other configured parts have no general fix short of replaying the feature
  tree; they are reported, not corrected. A configuration whose partition is
  empty falls back to one that has bodies; a part whose bodies match an
  inactive flag (e.g. "NO NUT" flagged active, body stored) converts what is
  stored.
- Fastener sizes outside the tables (BHCS M1.6, M2.2, M2.6, M3.5; SHCS
  above M24), unparseable names, and families other than SHCS/BHCS (hex nuts,
  for example) are excluded and reported under `--fasteners`, or reported as
  mismatches without it.
- Generated screws are generic: no threads, chamfers or under-head radius,
  socket depth 0.75 k, BHCS dome calibrated on one stored size. Fine for
  fit, mass and viewing; not the vendor's CAD.

**Without `--fasteners`.** `sldasm2step.py --exclude 'BHCS*' --exclude
'SHCS*'` leaves them out and lists every excluded instance with its
configuration, so the omission is reported rather than silent.

## 2. One planar-face edge can reach ±128 m

**Seen** while checking `.glb` bounding boxes against their STEP files: one
vendor part converts with one edge (on a `Geom_Plane` face) whose 3D curve
runs out to about ±128 m in X and Y although all its vertices are inside the
part. FreeCAD's bounding box of any STEP containing the part is therefore
~260 m wide. The tessellation is not affected (the `.glb` box is correct), so
it only matters for tools that trust the B-rep box. Not investigated further:
find the edge with `BRepBndLib.AddOptimal` per edge and look at its source
curve (likely an intersection or SP curve on a wrong branch or unclamped
range).
