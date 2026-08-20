# streaming-silence-detection

Minimal container that watches an Icecast (or any ffmpeg-readable) audio stream
and POSTs a webhook when the audio goes silent and when it comes back.

Alpine + ffmpeg + python3, stdlib only, non-root, read-only rootfs, one ffmpeg
process per container. Multi-arch: `linux/amd64` + `linux/arm64`.

```
docker run --rm \
  -e STREAM_URL=https://icecast.example.com/station-abc123 \
  -e WEBHOOK_URL=https://webhook.example.com/silence \
  -e SILENCE_DURATION=5 \
  -e WEBHOOK_EXTRA_STATION_SERIAL=ABC123STATION \
  ghcr.io/sectoroverload2k/streaming-silence-detection:main
```

### Dry run

With no `WEBHOOK_URL`, every event is logged to stdout as the JSON line that
would have been POSTed. Use this to sanity-check a stream and tune the threshold
before pointing it at a receiver:

```
docker run --rm \
  -e STREAM_URL=https://relay01.example.net/listen/mystation/mystation.mp3 \
  -e SILENCE_DURATION=5 \
  ghcr.io/sectoroverload2k/streaming-silence-detection:main
```

To measure what a stream is actually doing before choosing `SILENCE_THRESHOLD`:

```
docker run --rm --entrypoint ffmpeg ghcr.io/sectoroverload2k/streaming-silence-detection:main \
  -hide_banner -i "$STREAM_URL" -t 15 -af volumedetect -f null -
```

Digital silence reports around `mean_volume: -91 dB`; the `-50dB` default sits
well clear of normal programme level.

## Environment

| Var | Default | Meaning |
| --- | --- | --- |
| `STREAM_URL` | **required** | Stream to monitor (http/https, or anything ffmpeg can open) |
| `WEBHOOK_URL` | _unset_ | Where events are POSTed as JSON. Unset → events are logged to stdout only |
| `SILENCE_DURATION` | `5` | Seconds of silence before `silence_start` fires (3 / 5 / 10 …) |
| `SILENCE_THRESHOLD` | `-50dB` | Level counted as silence. Accepts ffmpeg `noise=` syntax (`-50dB`, `0.001`) |
| `SEND_STREAM_EVENTS` | `true` | Also send `stream_up` / `stream_down` |
| `STREAM_RECONNECT_DELAY` | `5` | Base seconds before restarting ffmpeg after it exits |
| `STREAM_RECONNECT_MAX_DELAY` | `60` | Ceiling for the linear reconnect backoff |
| `WEBHOOK_TIMEOUT` | `10` | Per-attempt HTTP timeout, seconds |
| `WEBHOOK_RETRIES` | `3` | Attempts per event before it is dropped |
| `WEBHOOK_RETRY_DELAY` | `2` | Base seconds between attempts (multiplied by attempt number) |
| `HEARTBEAT_FILE` | `/tmp/heartbeat` | Liveness file, rewritten while ffmpeg decodes audio |
| `HEARTBEAT_MAX_AGE` | `30` | Heartbeat age (seconds) at which the health check fails |
| `LOG_LEVEL` | `INFO` | `DEBUG` also logs every ffmpeg stderr line |
| `STREAM_USER_AGENT` | `streaming-silence-detection/1.0` | User-Agent sent to the stream |

### Stream authentication

For mounts protected by an htpasswd file:

| Var | Meaning |
| --- | --- |
| `STREAM_USERNAME` | HTTP basic auth user |
| `STREAM_PASSWORD` | HTTP basic auth password |
| `STREAM_PASSWORD_FILE` | Read the password from a file instead (mounted Secret) |
| `STREAM_HEADER_*` | Any extra request header: `STREAM_HEADER_X_TOKEN=abc` → `X-Token: abc` |

Credentials are sent as an `Authorization: Basic` header, not as `user:pass@host`
in the URL, so passwords containing `@ : / ?` need no escaping. If a URL with
embedded userinfo is used anyway, the password is redacted from logs and webhook
payloads.

### Webhook authentication

`WEBHOOK_HEADER_*` becomes a request header on the webhook POST:
`WEBHOOK_HEADER_X_API_KEY=abc` → `X-Api-Key: abc`.

### Extra fields

Every `WEBHOOK_EXTRA_*` var is added to the payload as a top-level field with the
prefix stripped:

```
WEBHOOK_EXTRA_STATION_SERIAL=ABC123STATION   ->   "STATION_SERIAL": "ABC123STATION"
WEBHOOK_EXTRA_SITE=eu-west-1                 ->   "SITE": "eu-west-1"
```

Names are passed through verbatim (case included). An extra that collides with a
built-in field is refused and logged, so it can never overwrite `event` or
`stream_url`.

## Events

| Event | When |
| --- | --- |
| `silence_start` | Audio has been below the threshold for `SILENCE_DURATION` |
| `silence_end` | Audio returned; includes how long the silence lasted |
| `stream_up` | ffmpeg opened the stream (`message` distinguishes first connect from reconnect) |
| `stream_down` | ffmpeg exited — connection lost, 401, bad mount. Sent once per outage, not once per retry |

```json
{
  "event": "silence_start",
  "stream_url": "https://icecast.example.com/station-abc123",
  "timestamp": "2026-08-17T14:47:55Z",
  "silence_threshold": "-50dB",
  "silence_duration_threshold_seconds": 5.0,
  "stream_position_seconds": 3.0,
  "detector_hostname": "silence-detect-station-abc123-7d9f",
  "message": "silence detected",
  "STATION_SERIAL": "ABC123STATION"
}
```

`silence_end` adds `silence_duration_seconds`; `stream_down` adds
`ffmpeg_exit_code` and, on a non-zero exit, `ffmpeg_error` with the last lines of
ffmpeg output (the 401 text shows up here when stream credentials are wrong).

Detection is inherently `SILENCE_DURATION` late: ffmpeg reports `silence_start`
only once the run of silence has lasted that long. `stream_position_seconds` is
the position where the silence actually began.

## Health

The monitor rewrites `HEARTBEAT_FILE` from ffmpeg's `-progress` output, which
only advances while audio is being decoded. `/usr/local/bin/healthcheck` fails
when that file goes stale, which catches a connection that hangs without closing
— wired up as both a Docker `HEALTHCHECK` and the Kubernetes liveness probe.

## Deploying

One container per stream; `replicas: 1` per Deployment, since a second replica
would duplicate every webhook.

```
kubectl apply -f k8s/deployment.yaml     # copy per stream, edit env
```

`k8s/secret.example.yaml` shows the Secret shape for stream credentials and the
webhook API key. `docker-compose.yml` covers the same thing outside Kubernetes.

## CI

`.github/workflows/docker.yml` builds `linux/amd64` + `linux/arm64` with buildx
and pushes to GHCR, then verifies the manifest actually contains both
architectures and runs `ci/smoke/run.sh` against each one. The smoke test serves
a tone → silence → tone file behind basic auth and asserts that `silence_start`,
`silence_end` and the `WEBHOOK_EXTRA_*` fields reach a stub webhook from inside
the image.

Tags: `sha-<12>` always, plus `main` on main, `pr-<n>` on pull requests, and
`<version>` + `latest` for a `v*` tag.

Run the smoke test against a local build:

```
docker build -t silence-detect:local .
IMAGE=silence-detect:local ci/smoke/run.sh
```

## License

[Elastic License 2.0](LICENSE). Free to use, modify and redistribute, including
inside a commercial organisation and for monitoring commercial streams. You may
not provide it to third parties as a hosted or managed service.

The published image also bundles ffmpeg (GPL-2.0-or-later AND
LGPL-2.1-or-later), Python (PSF-2.0) and the Alpine base system, each under its
own terms — see [NOTICE](NOTICE). Commercial terms: anthony@linsday.net.
