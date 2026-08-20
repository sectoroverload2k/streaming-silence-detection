#!/usr/bin/env python3
"""Webhook receiver for the smoke test: appends every POST body to a log file."""

import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

LOG_PATH = os.environ.get("HOOK_LOG", "/data/hook.log")


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8", "replace")
        with open(LOG_PATH, "a") as handle:
            handle.write(f"{self.headers.get('X-Api-Key', '-')} {body}\n")
        print(body, flush=True)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *_args):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9000
    HTTPServer(("", port), Handler).serve_forever()
