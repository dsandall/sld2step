// Converter worker: Pyodide + OCP.wasm + sldprt2xt, running the sld2step CLIs.
// Every file is served by the site itself (web/build.sh); nothing comes from a CDN.
// Messages in:  {type: "convert", files: [{path, data: ArrayBuffer}], fasteners}
// Messages out: {type: "stage" | "line" | "ready" | "done" | "error", ...}

import { loadPyodide } from "./pyodide/pyodide.mjs";

const here = (p) => new URL(p, self.location).href;
const PY_FILES = ["sld2step.py", "sldasm2step.py", "sld2glb.py", "sldload.py", "fasteners.py",
                  "webentry.py", "patches/sldprt2xt-1.2.0-legal_owners.patch"];

const post = (m, transfer) => self.postMessage(m, transfer || []);
// OCCT prints its own banners (ANSI-coloured) to the same stdout; keep only report lines.
const isReport = (s) => !s.includes("\x1b") && /^(OK|WARN|FAIL|    \S)/.test(s);

const ready = (async () => {
  const t0 = performance.now();
  post({ type: "stage", text: "Loading Python runtime" });
  const py = await loadPyodide({ indexURL: here("pyodide/") });
  py.setStdout({ batched: (s) => isReport(s) && post({ type: "line", text: s }) });
  py.setStderr({ batched: (s) => console.debug(s) });
  post({ type: "stage", text: "Loading geometry kernel (about 25 MB, cached after the first visit)" });
  await py.loadPackage("micropip");
  const wheels = await (await fetch("wheels/index.json")).json();
  await py.pyimport("micropip").install(wheels.map((w) => here("wheels/" + w)), { deps: false });
  post({ type: "stage", text: "Loading converter" });
  py.FS.mkdirTree("/app/patches");
  for (const f of PY_FILES) {
    const r = await fetch("py/" + f);
    if (!r.ok) throw new Error(`py/${f}: HTTP ${r.status}`);
    py.FS.writeFile("/app/" + f, new Uint8Array(await r.arrayBuffer()));
  }
  py.runPython("import sys; sys.path.insert(0, '/app'); import webentry; webentry.setup()");
  post({ type: "ready", seconds: (performance.now() - t0) / 1000 });
  return py;
})();
ready.catch((e) => post({ type: "error", text: String(e) }));

function rmTree(FS, dir) {
  if (!FS.analyzePath(dir).exists) return;
  for (const n of FS.readdir(dir)) {
    if (n === "." || n === "..") continue;
    const p = dir + "/" + n;
    FS.isDir(FS.stat(p).mode) ? rmTree(FS, p) : FS.unlink(p);
  }
  FS.rmdir(dir);
}

self.onmessage = async (ev) => {
  const m = ev.data;
  if (m.type !== "convert") return;
  let py;
  try { py = await ready; } catch { return; }
  const FS = py.FS;
  try {
    rmTree(FS, "/work");
    for (const f of m.files) {
      const p = "/work/in/" + f.path.replace(/^\/+/, "");
      FS.mkdirTree(p.slice(0, p.lastIndexOf("/")));
      FS.writeFile(p, new Uint8Array(f.data));
    }
    post({ type: "stage", text: "Converting" });
    const t0 = performance.now();
    const rc = py.globals.get("webentry").convert("/work/in", "/work/out", !!m.fasteners);
    const outputs = [];
    for (const n of FS.readdir("/work/out").sort()) {
      if (!/\.(step|glb)$/.test(n)) continue;
      const data = FS.readFile("/work/out/" + n).buffer;
      outputs.push({ name: n, data });
    }
    const memMB = Math.round((py._module?.HEAP8?.buffer?.byteLength || 0) / 1e6);   // diagnostics only
    post({ type: "done", rc, outputs, memMB, seconds: (performance.now() - t0) / 1000 },
         outputs.map((o) => o.data));
  } catch (e) {
    post({ type: "error", text: String(e) });
  } finally {
    try { rmTree(FS, "/work"); } catch {}
  }
};
