import sys, glob, json
import FreeCAD, Part
out = []
import os
for f in sorted(glob.glob(os.environ["STEPDIR"] + "/*.step")):
    try:
        sh = Part.read(f)
        info = {"file": f.split('/')[-1], "type": sh.ShapeType, "solids": len(sh.Solids), "shells": len(sh.Shells), "faces": len(sh.Faces), "valid": sh.isValid(), "closed": all(s.isClosed() for s in sh.Shells), "volume": round(sh.Volume, 3), "bbox": [round(x,3) for x in (sh.BoundBox.XMin, sh.BoundBox.YMin, sh.BoundBox.ZMin, sh.BoundBox.XMax, sh.BoundBox.YMax, sh.BoundBox.ZMax)]}
        if os.environ.get("NOBOP"):
            info["check"] = "skipped"
        else:
            try:
                sh.check(True)
                info["check"] = "ok"
            except Exception as e:
                info["check"] = str(e)[:600]
    except Exception as e:
        info = {"file": f, "error": str(e)}
    print(json.dumps(info))
