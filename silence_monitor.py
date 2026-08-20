#!/usr/bin/env python3
"""Silence detection for an Icecast (or any ffmpeg-readable) audio stream.

Runs ffmpeg's silencedetect filter against STREAM_URL, parses its stderr, and
POSTs a JSON webhook when silence starts and when it ends.

Standard library only - no pip dependencies, so the image stays small.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

LOG = logging.getLogger("silence-monitor")

# Keys the payload always sets. WEBHOOK_EXTRA_* values are refused if they
# would overwrite one of these, so a mis-set extra can never silently replace
# the event name or stream url.
RESERVED_KEYS = frozenset(
    {
        "event",
        "stream_url",
        "timestamp",
        "silence_threshold",
        "silence_duration_threshold_seconds",
        "silence_duration_seconds",
        "stream_position_seconds",
        "detector_hostname",
        "message",
        "ffmpeg_exit_code",
        "ffmpeg_error",
    }
)

SILENCE_START_RE = re.compile(r"silence_start:\s*(-?[\d.]+)")
SILENCE_END_RE = re.compile(
    r"silence_end:\s*(-?[\d.]+)\s*\|\s*silence_duration:\s*([\d.]+)"
)


def env_str(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.environ.get(name, default if default is not None else "").strip()
    if required and not value:
        sys.exit(f"{name} is required")
    return value


def env_num(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        sys.exit(f"{name} must be a number, got {raw!r}")


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def collect_prefixed(prefix: str) -> dict[str, str]:
    return {
        key[len(prefix) :]: value
        for key, value in os.environ.items()
        if key.startswith(prefix) and len(key) > len(prefix)
    }


def redact(url: str) -> str:
    """Strip userinfo credentials so stream URLs are safe to log."""
    parts = urlsplit(url)
    if not parts.password:
        return url
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    netloc = f"{parts.username or ''}:***@{host}"
    return urlunsplit(parts._replace(netloc=netloc))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


class Webhook:
    """Serializes webhook POSTs onto a worker thread.

    Delivery must never block the ffmpeg stderr reader: a webhook endpoint that
    hangs for its full timeout would otherwise delay detection of the next
    event, and a full stderr pipe would eventually stall ffmpeg itself.

    With no url configured, every event is logged as one JSON line to stdout
    instead - the same payload that would have been POSTed. That makes the
    container usable as a plain stream monitor, and is how you eyeball the
    threshold and duration settings for a new stream before wiring up a
    receiver.
    """

    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout: float,
        retries: int,
        retry_delay: float,
    ) -> None:
        self.url = url
        self.headers = headers
        self.timeout = timeout
        self.retries = max(1, int(retries))
        self.retry_delay = retry_delay
        self._queue: queue.Queue[dict | None] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="webhook", daemon=True)
        self._thread.start()

    def send(self, payload: dict) -> None:
        self._queue.put(payload)

    def drain(self, timeout: float = 15.0) -> None:
        self._queue.put(None)
        self._thread.join(timeout)

    def _run(self) -> None:
        while True:
            payload = self._queue.get()
            if payload is None:
                return
            self._post(payload)

    def _post(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        if not self.url:
            LOG.info("event (no WEBHOOK_URL set) %s", body.decode())
            return
        for attempt in range(1, self.retries + 1):
            request = urllib.request.Request(
                self.url, data=body, method="POST", headers=self.headers
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    LOG.info(
                        "webhook %s -> HTTP %s", payload["event"], response.status
                    )
                    return
            except urllib.error.HTTPError as exc:
                reason = f"HTTP {exc.code}"
            except Exception as exc:  # URLError, socket.timeout, ...
                reason = str(exc) or exc.__class__.__name__
            LOG.warning(
                "webhook %s failed (attempt %d/%d): %s",
                payload["event"],
                attempt,
                self.retries,
                reason,
            )
            if attempt < self.retries:
                time.sleep(self.retry_delay * attempt)
        LOG.error("webhook %s dropped after %d attempts", payload["event"], self.retries)


class Monitor:
    def __init__(self) -> None:
        self.stream_url = env_str("STREAM_URL", required=True)
        self.safe_url = redact(self.stream_url)
        self.silence_duration = env_num("SILENCE_DURATION", 5)
        self.silence_threshold = env_str("SILENCE_THRESHOLD", "-50dB")
        self.reconnect_delay = env_num("STREAM_RECONNECT_DELAY", 5)
        self.reconnect_max_delay = env_num("STREAM_RECONNECT_MAX_DELAY", 60)
        self.send_stream_events = env_bool("SEND_STREAM_EVENTS", True)
        self.heartbeat_file = env_str("HEARTBEAT_FILE", "/tmp/heartbeat")
        self.user_agent = env_str("STREAM_USER_AGENT", "streaming-silence-detection/1.0")
        self.stream_password = ""
        self.stream_headers = self._resolve_stream_headers()
        self.secrets = self._resolve_secrets()

        self.extras = self._resolve_extras()
        webhook_url = env_str("WEBHOOK_URL")
        if not webhook_url:
            LOG.warning("no WEBHOOK_URL set - events will only be logged to stdout")
        self.webhook = Webhook(
            webhook_url,
            headers=self._resolve_headers(),
            timeout=env_num("WEBHOOK_TIMEOUT", 10),
            retries=int(env_num("WEBHOOK_RETRIES", 3)),
            retry_delay=env_num("WEBHOOK_RETRY_DELAY", 2),
        )

        self.stopping = threading.Event()
        self.process: subprocess.Popen | None = None
        self.stream_up = False
        # A stream that stays dead reconnects forever; the webhook only hears
        # about the outage once, then again when it comes back.
        self.down_reported = False
        self.failures = 0

    # ---------------------------------------------------------------- config

    def _resolve_extras(self) -> dict[str, str]:
        extras = {}
        for key, value in sorted(collect_prefixed("WEBHOOK_EXTRA_").items()):
            if key in RESERVED_KEYS or key.lower() in RESERVED_KEYS:
                LOG.warning("ignoring WEBHOOK_EXTRA_%s: %s is a reserved key", key, key)
                continue
            extras[key] = value
        return extras

    def _resolve_headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "streaming-silence-detection/1.0",
        }
        # WEBHOOK_HEADER_X_API_KEY=abc -> "X-Api-Key: abc"
        for key, value in collect_prefixed("WEBHOOK_HEADER_").items():
            headers["-".join(part.capitalize() for part in key.split("_"))] = value
        return headers

    def _resolve_stream_headers(self) -> list[str]:
        """Extra HTTP request headers for the stream.

        Icecast mounts protected by an htpasswd file want HTTP Basic auth. The
        credentials are sent as an explicit Authorization header rather than as
        `user:pass@host` userinfo in the URL, so passwords containing `@`, `/`
        or `:` need no percent-encoding by whoever sets the env var.
        """
        headers: list[str] = []
        username = os.environ.get("STREAM_USERNAME", "")
        password = os.environ.get("STREAM_PASSWORD", "")
        password_file = env_str("STREAM_PASSWORD_FILE")
        if password_file:
            try:
                with open(password_file) as handle:
                    password = handle.read().strip()
            except OSError as exc:
                sys.exit(f"cannot read STREAM_PASSWORD_FILE: {exc}")
        self.stream_password = password
        if username or password:
            token = base64.b64encode(f"{username}:{password}".encode()).decode()
            headers.append(f"Authorization: Basic {token}")
            LOG.info("using HTTP basic auth for the stream (user=%s)", username)
        # STREAM_HEADER_X_TOKEN=abc -> "X-Token: abc"
        for key, value in sorted(collect_prefixed("STREAM_HEADER_").items()):
            headers.append(
                f"{'-'.join(part.capitalize() for part in key.split('_'))}: {value}"
            )
        return headers

    def _resolve_secrets(self) -> list[tuple[str, str]]:
        """Literal strings that must never reach a log line or a webhook."""
        secrets = []
        if self.safe_url != self.stream_url:
            secrets.append((self.stream_url, self.safe_url))
        for value in (urlsplit(self.stream_url).password, self.stream_password):
            if value:
                secrets.append((value, "***"))
        return secrets

    def scrub(self, text: str) -> str:
        """Redact stream credentials from ffmpeg output.

        ffmpeg echoes the input URL verbatim in its error messages, so a URL
        with embedded userinfo would otherwise put the password into the logs
        and into the ffmpeg_error webhook field - the one path that redact()
        never sees.
        """
        for secret, replacement in self.secrets:
            text = text.replace(secret, replacement)
        return text

    def ffmpeg_command(self) -> list[str]:
        command = [
            "ffmpeg", "-hide_banner", "-nostdin", "-nostats",
            "-loglevel", "info", "-progress", "pipe:1",
        ]
        if self.stream_url.startswith(("http://", "https://")):
            # ffmpeg's own reconnect handling covers brief drops without losing
            # the process; a full restart only happens if this gives up.
            command += [
                "-user_agent",
                self.user_agent,
                "-reconnect",
                "1",
                "-reconnect_streamed",
                "1",
                "-reconnect_delay_max",
                str(int(self.reconnect_delay)),
            ]
            if self.stream_headers:
                command += ["-headers", "".join(f"{h}\r\n" for h in self.stream_headers)]
        command += [
            "-i",
            self.stream_url,
            "-vn",
            "-af",
            f"silencedetect=noise={self.silence_threshold}:d={self.silence_duration}",
            "-f",
            "null",
            "-",
        ]
        return command

    # ---------------------------------------------------------------- events

    def emit(self, event: str, **fields) -> None:
        payload = {
            "event": event,
            "stream_url": self.safe_url,
            "timestamp": now_iso(),
            "silence_threshold": self.silence_threshold,
            "silence_duration_threshold_seconds": self.silence_duration,
            "detector_hostname": os.environ.get("HOSTNAME", ""),
        }
        payload.update(fields)
        payload.update(self.extras)
        self.webhook.send(payload)

    def touch_heartbeat(self) -> None:
        if not self.heartbeat_file:
            return
        try:
            with open(self.heartbeat_file, "w") as handle:
                handle.write(now_iso())
        except OSError as exc:
            LOG.warning("could not write heartbeat file: %s", exc)

    def _read_progress(self, stdout) -> None:
        """ffmpeg -progress output is the liveness signal: it only advances
        while audio is actually being decoded."""
        for line in stdout:
            if line.startswith("out_time_ms=") and not self.stopping.is_set():
                self.touch_heartbeat()

    def handle_stderr_line(self, line: str) -> None:
        # silencedetect flushes a closing silence_end when ffmpeg shuts down.
        # During our own shutdown that is an artefact of the terminate, not a
        # real return of audio, so nothing after SIGTERM is reported.
        if self.stopping.is_set():
            return

        match = SILENCE_START_RE.search(line)
        if match:
            position = float(match.group(1))
            LOG.warning("silence started at stream position %.2fs", position)
            self.emit("silence_start", stream_position_seconds=position,
                      message="silence detected")
            return

        match = SILENCE_END_RE.search(line)
        if match:
            position, duration = float(match.group(1)), float(match.group(2))
            LOG.info("silence ended after %.2fs of silence", duration)
            self.emit(
                "silence_end",
                stream_position_seconds=position,
                silence_duration_seconds=duration,
                message="silence ended",
            )
            return

        if line:
            LOG.debug("ffmpeg: %s", line)

    # ------------------------------------------------------------------- run

    def run_ffmpeg_once(self) -> int:
        command = self.ffmpeg_command()
        LOG.info("starting ffmpeg for %s", self.safe_url)
        self.process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        progress_reader = threading.Thread(
            target=self._read_progress, args=(self.process.stdout,),
            name="progress", daemon=True,
        )
        progress_reader.start()

        tail: list[str] = []
        for raw in self.process.stderr:
            line = self.scrub(raw.strip())
            if not self.stream_up and line.startswith("Input #0"):
                self.stream_up = True
                self.failures = 0
                self.touch_heartbeat()
                LOG.info("stream connected")
                if self.send_stream_events:
                    self.emit(
                        "stream_up",
                        message="stream reconnected" if self.down_reported
                        else "stream connected",
                    )
                self.down_reported = False
            tail = (tail + [line])[-5:]
            self.handle_stderr_line(line)

        code = self.process.wait()
        self.process = None
        if not self.stopping.is_set():
            detail = " | ".join(tail)
            log = LOG.info if code == 0 else LOG.error
            log("ffmpeg exited with code %s: %s", code, detail)
            self.failures += 1
            if self.send_stream_events and not self.down_reported:
                fields = {"ffmpeg_exit_code": code}
                if code != 0:
                    fields["ffmpeg_error"] = detail
                self.emit(
                    "stream_down",
                    message=f"stream reader exited (code {code})",
                    **fields,
                )
                self.down_reported = True
        self.stream_up = False
        return code

    def run(self) -> int:
        LOG.info(
            "silence threshold=%s duration=%ss extras=%s",
            self.silence_threshold,
            self.silence_duration,
            ",".join(self.extras) or "none",
        )
        while not self.stopping.is_set():
            try:
                self.run_ffmpeg_once()
            except FileNotFoundError:
                LOG.error("ffmpeg not found on PATH")
                return 127
            if self.stopping.is_set():
                break
            # Linear backoff so a stream that is down for hours is not polled
            # every few seconds; reset to the base delay on a successful connect.
            delay = min(self.reconnect_delay * max(1, self.failures), self.reconnect_max_delay)
            LOG.info("reconnecting in %ss", delay)
            self.stopping.wait(delay)
        self.webhook.drain()
        return 0

    def stop(self, signum, _frame) -> None:
        if self.stopping.is_set():
            return
        LOG.info("received signal %s, shutting down", signum)
        self.stopping.set()
        process = self.process
        if process and process.poll() is None:
            process.terminate()


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    monitor = Monitor()
    signal.signal(signal.SIGTERM, monitor.stop)
    signal.signal(signal.SIGINT, monitor.stop)
    return monitor.run()


if __name__ == "__main__":
    sys.exit(main())
