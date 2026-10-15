"""Serve web/dist locally with the response headers from web/_headers.

    python3 web/serve.py [port]      (default 8765, bound to 127.0.0.1)
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
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), functools.partial(Handler, directory=str(DIST)))
    print(f"serving {DIST} on http://127.0.0.1:{port}/")
    srv.serve_forever()
