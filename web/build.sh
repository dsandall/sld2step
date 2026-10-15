#!/usr/bin/env bash
# Assemble the static site in web/dist. Everything the page loads is served from
# dist itself: the page, the worker, the converter's own Python modules (the same
# files as the command-line tool), Pyodide, the wheels and three.js. Nothing is
# fetched from a third party at runtime. Needs `npm ci` in the repo root first.
set -euo pipefail
cd "$(dirname "$0")"
root=..
[ -f "$root/node_modules/pyodide/pyodide.mjs" ] || { echo "run npm ci in the repo root first"; exit 1; }

# downloads, verified against vendor.txt and cached in .vendor/
mkdir -p .vendor
grep -v '^#' vendor.txt | while read -r sum url; do
  f=".vendor/$(basename "$url")"
  [ -f "$f" ] || curl -fsSL -o "$f" "$url"
  [ "$(sha256sum "$f" | cut -d' ' -f1)" = "$sum" ] || { echo "checksum mismatch: $f"; rm -f "$f"; exit 1; }
done

rm -rf dist
mkdir -p dist/py/patches dist/pyodide dist/wheels dist/vendor/three/addons/{controls,loaders,utils}
commit=$(git -C "$root" rev-parse HEAD)
[ -z "$(git -C "$root" status --porcelain -- '*.py' patches web)" ] || echo "warning: uncommitted changes; the footer's source link points at $commit"
sed "s/__COMMIT__/$commit/g" index.html > dist/index.html
cp worker.js _headers dist/
cp webentry.py dist/py/
cp "$root"/{sld2step,sldasm2step,sld2glb,sldload,fasteners}.py dist/py/
cp "$root"/patches/*.patch dist/py/patches/
cp "$root"/node_modules/pyodide/{pyodide.mjs,pyodide.asm.mjs,pyodide.asm.wasm,python_stdlib.zip,pyodide-lock.json} dist/pyodide/
cp .vendor/micropip-*.whl dist/pyodide/
cp .vendor/*.whl dist/wheels/ && rm dist/wheels/micropip-*.whl
(cd dist/wheels && ls *.whl | python3 -c 'import json, sys; print(json.dumps(sys.stdin.read().split()))' > index.json)
t="$root/node_modules/three"
cp "$t"/build/{three.module.js,three.core.js} dist/vendor/three/
cp "$t"/examples/jsm/controls/OrbitControls.js dist/vendor/three/addons/controls/
cp "$t"/examples/jsm/loaders/GLTFLoader.js dist/vendor/three/addons/loaders/
cp "$t"/examples/jsm/utils/BufferGeometryUtils.js dist/vendor/three/addons/utils/
cp ../LICENSE dist/LICENSE.txt
cp third-party.txt dist/
echo "ok: web/dist $(du -sh dist | cut -f1), commit ${commit:0:7}; serve it with: python3 web/serve.py"
