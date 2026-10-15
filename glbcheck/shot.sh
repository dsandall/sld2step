#!/usr/bin/env bash
# Headless-Chrome check of a .glb: glbcheck/shot.sh file.glb out.png [view x,y,z]
# Serves the page and the .glb from a temp folder next to the .glb on localhost,
# prints the page's summary JSON, and saves a screenshot.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
glb=$(realpath "$1"); png=$(realpath -m "$2"); view=${3:-1,0.8,1.3}
[ -f "$here/bundle.js" ] || (cd "$here/.." && npx esbuild glbcheck/entry.mjs --bundle --format=esm --outfile=glbcheck/bundle.js --log-level=warning)
mkdir -p "$(dirname "$png")"
dir=$(mktemp -d "$(dirname "$glb")/.shot-XXXX"); trap 'kill $srv 2>/dev/null; rm -rf "$dir"' EXIT
cp "$here/view.html" "$here/bundle.js" "$dir/"; ln -s "$glb" "$dir/m.glb"
port=$((20000 + RANDOM % 20000))
python3 -m http.server "$port" --bind 127.0.0.1 -d "$dir" >/dev/null 2>&1 & srv=$!
sleep 0.5
url="http://127.0.0.1:$port/view.html?glb=m.glb&view=$view"
chrome=(google-chrome-stable --headless=new --password-store=basic --no-first-run --disable-sync
        --user-data-dir="$dir/profile" --enable-unsafe-swiftshader --use-angle=swiftshader
        --window-size=1400,1000 --virtual-time-budget=60000)
"${chrome[@]}" --dump-dom "$url" 2>/dev/null | sed -n 's:.*<pre id="out">\(.*\)</pre>.*:\1:p'
"${chrome[@]}" --screenshot="$png" "$url" >/dev/null 2>&1
echo "screenshot: $png"
