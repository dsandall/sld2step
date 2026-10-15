#!/usr/bin/env bash
# Download the public test set listed in manifest.tsv (MIT-licensed files from
# three GitHub repositories, each pinned to a commit) and verify every SHA-256.
# The CAD files themselves are never committed (see .gitignore).
#   manifest.tsv: <owner/repo@commit> <path in repo> <local path> <sha256>
set -euo pipefail
cd "$(dirname "$0")"
fail=0
while IFS=$'\t' read -r src path dst sum; do
  repo=${src%@*}; rev=${src#*@}
  url="https://raw.githubusercontent.com/$repo/$rev/$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1]))' "$path")"
  mkdir -p "$(dirname "$dst")"
  [ -f "$dst" ] || curl -fsSL -o "$dst" "$url"
  if [ "$(sha256sum "$dst" | cut -d' ' -f1)" != "$sum" ]; then echo "BAD $dst"; rm -f "$dst"; fail=1; fi
done < manifest.tsv
[ $fail = 0 ] && echo "ok: $(wc -l < manifest.tsv) files in $(pwd)" || { echo "checksum mismatch"; exit 1; }
