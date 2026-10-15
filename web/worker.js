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
  py.setStdout({ batched: (s) => {
    if (s.startsWith(">> ")) post({ type: "progress", text: s.slice(3) });
    else if (isReport(s)) post({ type: "line", text: s });
  } });
  py.setStderr({ batched: (s) => console.debug(s) });
  post({ type: "stage", text: "Downloading the geometry kernel" });
  await py.loadPackage("micropip");
  // Fetch the wheels here (not inside micropip) so the download can report bytes.
  const wheels = await (await fetch("wheels/index.json")).json();   // [{name, size}]
  const total = wheels.reduce((a, w) => a + w.size, 0);
  let loaded = 0, lastPost = 0;
  py.FS.mkdirTree("/wheels");
  for (const w of wheels) {
    const r = await fetch("wheels/" + w.name);
    if (!r.ok) throw new Error(`wheels/${w.name}: HTTP ${r.status}`);
    const buf = new Uint8Array(w.size);
    let at = 0;
    const reader = r.body.getReader();
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      if (at + value.length > buf.length) throw new Error(`wheels/${w.name}: larger than index.json says`);
      buf.set(value, at); at += value.length; loaded += value.length;
      if (performance.now() - lastPost > 200) { lastPost = performance.now(); post({ type: "download", loaded, total }); }
    }
    py.FS.writeFile("/wheels/" + w.name, buf.subarray(0, at));
  }
  post({ type: "download", loaded: total, total });
  post({ type: "stage", text: "Installing the geometry kernel" });
  await py.pyimport("micropip").install(wheels.map((w) => "emfs:/wheels/" + w.name), { deps: false });
  for (const w of wheels) py.FS.unlink("/wheels/" + w.name);
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
