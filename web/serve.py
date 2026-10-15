"""Serve web/dist locally with the response headers from web/_headers.

    python3 web/serve.py [port] [bind]      (default 8765 on 127.0.0.1; bind 0.0.0.0 for the LAN)
"""
import fnmatch
import functools
import http.server
import sys
from pathlib import Path

DIST = Path(__file__).parent / "dist"


def rules():
    out, pat = [], None
    for line in (DIST / "_headers").read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():
            pat = line.strip(); out.append((pat, []))
        else:
            k, v = line.strip().split(":", 1); out[-1][1].append((k.strip(), v.strip()))
    return out


class Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                      ".mjs": "text/javascript", ".wasm": "application/wasm", ".whl": "application/zip"}

    def end_headers(self):
        path = self.path.split("?", 1)[0]
        cached = False
        for pat, hdrs in rules():
            if fnmatch.fnmatch(path, pat):
                for k, v in hdrs:
                    self.send_header(k, v)
                    cached |= k.lower() == "cache-control"
        if not cached:
            self.send_header("Cache-Control", "no-cache")   # else browsers guess from Last-Modified
        super().end_headers()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    bind = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"
    srv = http.server.ThreadingHTTPServer((bind, port), functools.partial(Handler, directory=str(DIST)))
    print(f"serving {DIST} on http://{bind}:{port}/")
    srv.serve_forever()
