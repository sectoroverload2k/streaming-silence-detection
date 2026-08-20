#!/usr/bin/env python3
"""Static file server that demands HTTP basic auth.

Stands in for an Icecast mount protected by an htpasswd file, so the smoke test
covers STREAM_USERNAME / STREAM_PASSWORD as well as silence detection itself.
"""

import base64
import os
import sys
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler

EXPECTED = "Basic " + base64.b64encode(
    f"{os.environ.get('AUTH_USER', 'stream')}:"
    f"{os.environ.get('AUTH_PASS', 'p@ss:word/1')}".encode()
).decode()


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.headers.get("Authorization") != EXPECTED:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Icecast"')
            self.end_headers()
            return
        super().do_GET()

    def log_message(self, fmt, *args):
        print(fmt % args, flush=True)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    directory = sys.argv[2] if len(sys.argv) > 2 else "/data"
    HTTPServer(("", port), partial(Handler, directory=directory)).serve_forever()
