#!/usr/bin/env bash
# Create the virtualenv, install the two dependencies, apply the sldprt2xt patch.
set -euo pipefail
cd "$(dirname "$0")"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
site=$(.venv/bin/python -c "import sldprt2xt, os; print(os.path.dirname(sldprt2xt.__file__))")
if ! grep -q "_fit_legal_owners" "$site/xt/edit.py"; then
  patch -p0 -d "$site/.." < patches/sldprt2xt-1.2.0-legal_owners.patch
fi
if command -v npm >/dev/null; then
  npm ci --no-fund --no-audit    # gltfpack (for --glb), three + esbuild (glbcheck/)
else
  echo "warning: npm not found; --glb needs gltfpack (npm ci in this folder)"
fi
echo "ok: .venv ready ($site)"
echo "usage: .venv/bin/python sld2step.py <part.SLDPRT|dir> -o out/"
echo "       .venv/bin/python sldasm2step.py <asm.SLDASM> -o out/"
