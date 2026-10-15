import sys
from OCP.STEPControl import STEPControl_Reader
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.TopExp import TopExp_Explorer
from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID
from OCP.TopoDS import TopoDS
from OCP.BRep import BRep_Tool
from OCP.TopLoc import TopLoc_Location
r = STEPControl_Reader(); r.ReadFile(sys.argv[1]); r.TransferRoots(); shape = r.OneShape()
BRepMesh_IncrementalMesh(shape, 0.8, False, 0.5, True)
out = open(sys.argv[2], "w"); sid = 0
ex = TopExp_Explorer(shape, TopAbs_SOLID)
solids = []
while ex.More(): solids.append(ex.Current()); ex.Next()
if not solids: solids = [shape]
for sid, sol in enumerate(solids):
    fx = TopExp_Explorer(sol, TopAbs_FACE)
    while fx.More():
        f = TopoDS.Face_s(fx.Current()); loc = TopLoc_Location(); tri = BRep_Tool.Triangulation_s(f, loc)
        if tri is not None:
            trsf = loc.Transformation()
            for i in range(1, tri.NbTriangles() + 1):
                a, b, c = tri.Triangle(i).Get()
                pts = [tri.Node(k).Transformed(trsf) for k in (a, b, c)]
                out.write(f"{sid} " + " ".join(f"{p.X():.3f} {p.Y():.3f} {p.Z():.3f}" for p in pts) + "\n")
        fx.Next()
print("solids", len(solids))
