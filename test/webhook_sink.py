#!/usr/bin/env python3
"""Proof-of-concept webhook receiver for manual testing.

Accepts any POST, pretty-prints the request to stdout and appends one JSON
line per delivery to a log file. Nothing is validated - it exists so you can
watch what the detector actually sends before wiring up a real endpoint.

    python3 test/webhook_sink.py [PORT] [LOGFILE]

Environment:
    HOOK_LOG      log file path (default: ./hook.log, or argv[2])
    HOOK_STATUS   HTTP status to return (default 200; set 500 to watch the
                  detector's retry/backoff behaviour)
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

LOG_PATH = os.environ.get("HOOK_LOG") or (
    sys.argv[2] if len(sys.argv) > 2 else "hook.log"
)
STATUS = int(os.environ.get("HOOK_STATUS", "200"))

# Headers worth showing: whatever the detector was told to send plus the
# defaults it always sets.
INTERESTING = ("content-type", "user-agent")


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8", "replace")

        try:
            payload = json.loads(raw)
            pretty = json.dumps(payload, indent=2, sort_keys=True)
            event = payload.get("event", "?")
        except json.JSONDecodeError:
            payload, pretty, event = None, raw, "non-json"

        custom = {
            key: value
            for key, value in self.headers.items()
            if key.lower() in INTERESTING or key.lower().startswith("x-")
        }

        print(f"\n=== {stamp()}  POST {self.path}  event={event}", flush=True)
        for key, value in sorted(custom.items()):
            print(f"    {key}: {value}", flush=True)
        print(pretty, flush=True)

        try:
            with open(LOG_PATH, "a") as handle:
                handle.write(
                    json.dumps(
                        {
                            "received_at": stamp(),
                            "path": self.path,
                            "headers": custom,
                            "body": payload if payload is not None else raw,
                        }
                    )
                    + "\n"
                )
        except OSError as exc:
            print(f"    (could not write {LOG_PATH}: {exc})", flush=True)

        body = b"ok" if STATUS < 400 else b"forced failure"
        self.send_response(STATUS)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        # So `curl http://host:9000/` tells you the sink is alive.
        body = f"webhook sink alive, logging to {LOG_PATH}\n".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9000
    print(
        f"webhook sink listening on :{port}, appending to {LOG_PATH}, "
        f"replying HTTP {STATUS}",
        flush=True,
    )
    try:
        HTTPServer(("", port), Handler).serve_forever()
    except KeyboardInterrupt:
        pass
