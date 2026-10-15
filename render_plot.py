import sys, numpy as np, matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.collections import PolyCollection
d = np.loadtxt(sys.argv[1]); sid = d[:,0].astype(int); t = d[:,1:].reshape(-1,3,3)
fig, axes = plt.subplots(1, 3, figsize=(21, 7))
cols = plt.cm.tab20(sid % 20)
for ax, (i, j, name) in zip(axes, ((0,1,"top XY"),(0,2,"front XZ"),(1,2,"side YZ"))):
    pc = PolyCollection(t[:, :, [i, j]], facecolors=cols, edgecolors="none", alpha=0.9); ax.add_collection(pc)
    ax.autoscale(); ax.set_aspect("equal"); ax.set_title(f"{name}  ({len(np.unique(sid))} solids)"); ax.grid(True, alpha=0.3)
plt.tight_layout(); plt.savefig(sys.argv[2], dpi=80); print("saved", sys.argv[2])
