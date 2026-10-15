import os, json, sys
import FreeCAD, Part
f = os.environ["STEPFILE"]
try:
    sh = Part.read(f)
    info = {"file": os.path.basename(f), "type": sh.ShapeType, "solids": len(sh.Solids), "shells": len(sh.Shells), "faces": len(sh.Faces),
            "valid": sh.isValid(), "closed": all(s.isClosed() for s in sh.Shells), "volume": round(sh.Volume, 3),
            "bbox": [round(x, 3) for x in (sh.BoundBox.XMin, sh.BoundBox.YMin, sh.BoundBox.ZMin, sh.BoundBox.XMax, sh.BoundBox.YMax, sh.BoundBox.ZMax)]}
    if os.environ.get("NOBOP") or len(sh.Faces) > 3000:
        info["check"] = "skipped"
    else:
        try:
            sh.check(True); info["check"] = "ok"
        except Exception as e:
            info["check"] = str(e)[:300]
except Exception as e:
    info = {"file": os.path.basename(f), "error": str(e)[:300]}
out = os.environ.get("JSONOUT")
line = json.dumps(info)
if out:
    open(out, "a").write(line + "\n")
print(line)
