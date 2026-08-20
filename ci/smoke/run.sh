#!/usr/bin/env bash
# End-to-end smoke test for the silence detector image.
#
# Builds a tone → silence → tone audio file, serves it over HTTP behind basic
# auth, points the detector at it, and asserts that silence_start and
# silence_end reach a stub webhook with the WEBHOOK_EXTRA_* fields attached.
#
#   IMAGE=ghcr.io/sectoroverload2k/streaming-silence-detection:sha-abc ci/smoke/run.sh
#   IMAGE=... PLATFORM=linux/arm64 ci/smoke/run.sh
set -euo pipefail

IMAGE="${IMAGE:?set IMAGE to the image under test}"
PLATFORM="${PLATFORM:-}"
NET="silence-smoke-$$"
WORK="$(mktemp -d)"
PLATFORM_ARGS=()
[ -n "$PLATFORM" ] && PLATFORM_ARGS=(--platform "$PLATFORM")

STREAM_USER=stream
STREAM_PASS='p@ss:word/1'   # deliberately full of URL-hostile characters

cleanup() {
    docker rm -f "$NET-hook" "$NET-web" "$NET-mon" >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT

chmod 777 "$WORK"
cp "$(dirname "$0")"/*.py "$WORK/"
: > "$WORK/hook.log"
chmod 666 "$WORK/hook.log"

docker network create "$NET" >/dev/null

echo "== generating 3s tone / 6s silence / 3s tone"
docker run --rm "${PLATFORM_ARGS[@]}" -v "$WORK:/data" --entrypoint ffmpeg "$IMAGE" \
    -hide_banner -loglevel error \
    -f lavfi -i "sine=f=440:r=44100:d=3" \
    -f lavfi -i "anullsrc=r=44100:cl=stereo:d=6" \
    -f lavfi -i "sine=f=440:r=44100:d=3" \
    -filter_complex "[0:a][1:a][2:a]concat=n=3:v=0:a=1" \
    -ac 2 -c:a libmp3lame -y /data/stream.mp3
test -s "$WORK/stream.mp3"

echo "== starting webhook stub"
docker run -d --name "$NET-hook" --network "$NET" "${PLATFORM_ARGS[@]}" \
    -v "$WORK:/data" -e HOOK_LOG=/data/hook.log \
    --entrypoint python3 "$IMAGE" /data/webhook_stub.py 9000 >/dev/null

echo "== starting authenticated stream server"
docker run -d --name "$NET-web" --network "$NET" "${PLATFORM_ARGS[@]}" \
    -v "$WORK:/data" -e AUTH_USER="$STREAM_USER" -e AUTH_PASS="$STREAM_PASS" \
    --entrypoint python3 "$IMAGE" /data/auth_stream_server.py 8000 /data >/dev/null

echo "== starting detector"
docker run -d --name "$NET-mon" --network "$NET" "${PLATFORM_ARGS[@]}" \
    -e STREAM_URL="http://$NET-web:8000/stream.mp3" \
    -e STREAM_USERNAME="$STREAM_USER" \
    -e STREAM_PASSWORD="$STREAM_PASS" \
    -e WEBHOOK_URL="http://$NET-hook:9000/" \
    -e WEBHOOK_HEADER_X_API_KEY=smoke-key \
    -e SILENCE_DURATION=3 \
    -e WEBHOOK_EXTRA_STATION_SERIAL=ABC123STATION \
    "$IMAGE" >/dev/null

echo "== waiting for silence_start and silence_end"
for _ in $(seq 1 60); do
    if grep -q silence_start "$WORK/hook.log" && grep -q silence_end "$WORK/hook.log"; then
        break
    fi
    sleep 1
done

echo "--- detector log"
docker logs "$NET-mon" 2>&1 | sed 's/^/   /'
echo "--- webhook log"
sed 's/^/   /' "$WORK/hook.log"

fail=0
check() {
    if grep -q "$1" "$WORK/hook.log"; then
        echo "ok: $2"
    else
        echo "FAIL: $2"
        fail=1
    fi
}
check '"event": "silence_start"'                  "silence_start delivered"
check '"event": "silence_end"'                    "silence_end delivered"
check '"STATION_SERIAL": "ABC123STATION"'         "WEBHOOK_EXTRA_* passed through"
check '^smoke-key '                               "WEBHOOK_HEADER_* sent as X-Api-Key"
check '"silence_duration_seconds"'                "silence duration reported"
grep -q '"stream_url": "http://[^"]*stream.mp3"' "$WORK/hook.log" \
    && echo "ok: stream url reported" || { echo "FAIL: stream url reported"; fail=1; }

if [ "$fail" -eq 0 ]; then
    echo "ALL_SMOKE_CHECKS_OK"
else
    exit 1
fi
