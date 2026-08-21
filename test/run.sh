#!/usr/bin/env bash
# Manual test harness for the silence detector image.
#
# Unlike ci/smoke/run.sh - which asserts and exits - these scenarios are meant
# to be watched: they stream the detector's log (and the webhook sink's) to
# your terminal until DURATION elapses or you hit Ctrl-C.
#
#   test/run.sh log                     # detector -> stdout only
#   test/run.sh webhook                 # detector -> local webhook sink
#   test/run.sh silence                 # synthesized tone/silence/tone source
#   test/run.sh retry                   # sink returns HTTP 500, watch retries
#   test/run.sh down                    # unreachable stream, watch backoff
#
# Any scenario takes an optional stream URL as its second argument:
#
#   test/run.sh webhook https://example.com/stream.mp3
#
# Environment:
#   IMAGE            image under test         (default silence-detect:local)
#   DURATION         seconds to watch, 0 = until Ctrl-C   (default 60)
#   SILENCE_DURATION seconds of silence before an event   (default 5)
#   SILENCE_THRESHOLD                                     (default -50dB)
#   STREAM_USERNAME / STREAM_PASSWORD  basic auth for the stream, if needed
#   KEEP=1           leave containers running after the script exits
set -euo pipefail

SCENARIO="${1:-help}"
# Matches test/make-test-stream.sh, so PORT/NAME set for one apply to both.
DEFAULT_URL="http://localhost:${PORT:-8123}/${NAME:-my-test-stream.mp3}"
URL="${2:-${STREAM_URL:-$DEFAULT_URL}}"

IMAGE="${IMAGE:-silence-detect:local}"
DURATION="${DURATION:-60}"
SILENCE_DURATION="${SILENCE_DURATION:-5}"
SILENCE_THRESHOLD="${SILENCE_THRESHOLD:--50dB}"
KEEP="${KEEP:-0}"

RUN="silence-test-$$"
HOST_ARGS=()
NET="$RUN-net"
WORK="$(mktemp -d)"
HOOK_LOG="$WORK/hook.log"
CONTAINERS=()

cleanup() {
    trap - EXIT INT TERM
    if [ "$KEEP" = "1" ]; then
        echo
        echo "KEEP=1: leaving ${CONTAINERS[*]:-nothing} and network $NET in place"
        echo "        webhook log: $HOOK_LOG"
        echo "        remove with: docker rm -f ${CONTAINERS[*]:-} ; docker network rm $NET"
        return
    fi
    echo
    echo "== cleaning up"
    if [ "${#CONTAINERS[@]}" -gt 0 ]; then
        docker rm -f "${CONTAINERS[@]}" >/dev/null 2>&1 || true
    fi
    docker network rm "$NET" >/dev/null 2>&1 || true
    if [ -s "$HOOK_LOG" ]; then
        echo "== webhook deliveries recorded: $(wc -l < "$HOOK_LOG")"
        cp "$HOOK_LOG" "./hook-$RUN.log" 2>/dev/null \
            && echo "   saved to ./hook-$RUN.log"
    fi
    rm -rf "$WORK"
}
trap cleanup EXIT INT TERM

need_image() {
    if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
        echo "== image $IMAGE not found, building"
        docker build -t "$IMAGE" "$(dirname "$0")/.."
    fi
}

start_sink() {
    local status="${1:-200}"
    echo "== starting webhook sink (replies HTTP $status, log $HOOK_LOG)"
    docker run -d --name "$RUN-hook" --network "$NET" \
        -v "$WORK:/data" -e HOOK_LOG=/data/hook.log -e HOOK_STATUS="$status" \
        --entrypoint python3 "$IMAGE" /data/webhook_sink.py 9000 >/dev/null
    CONTAINERS+=("$RUN-hook")
}

# Serves a tone -> silence -> tone mp3 over plain HTTP, so silence events fire
# on a schedule you control instead of whenever the real stream happens to dip.
start_local_stream() {
    IMAGE="$IMAGE" TONE=5 SILENCE=10 \
        "$(dirname "$0")/make-test-stream.sh" file "$WORK/stream.mp3"
    echo "== serving it on http://$RUN-web:8000/stream.mp3"
    docker run -d --name "$RUN-web" --network "$NET" -v "$WORK:/data" \
        --entrypoint python3 "$IMAGE" -m http.server 8000 --directory /data >/dev/null
    CONTAINERS+=("$RUN-web")
    URL="http://$RUN-web:8000/stream.mp3"
}

# localhost inside the detector container is the container, not your machine.
# Anything pointed at localhost/127.0.0.1 is redirected to the docker host so a
# stream you are serving locally is actually reachable.
hostify() {
    case "$URL" in
        *//localhost:*|*//localhost/|*//127.0.0.1:*|*//127.0.0.1/)
            URL="$(printf '%s' "$URL" | sed -E 's#//(localhost|127\.0\.0\.1)([:/])#//host.docker.internal\2#')"
            HOST_ARGS=(--add-host host.docker.internal:host-gateway)
            echo "== stream URL is host-local, redirecting to $URL"
            ;;
    esac
}

start_detector() {
    local webhook_url="${1:-}"
    local safe_url
    safe_url="$(printf '%s' "$URL" | sed -E 's#://[^/@]*:[^/@]*@#://***:***@#')"
    echo "== starting detector against $safe_url"
    local args=(
        -e STREAM_URL="$URL"
        -e SILENCE_DURATION="$SILENCE_DURATION"
        -e SILENCE_THRESHOLD="$SILENCE_THRESHOLD"
        -e LOG_LEVEL="${LOG_LEVEL:-INFO}"
    )
    if [ -n "$webhook_url" ]; then args+=(-e WEBHOOK_URL="$webhook_url"); fi
    if [ -n "${STREAM_USERNAME:-}" ]; then args+=(-e STREAM_USERNAME="$STREAM_USERNAME"); fi
    if [ -n "${STREAM_PASSWORD:-}" ]; then args+=(-e STREAM_PASSWORD="$STREAM_PASSWORD"); fi
    docker run -d --name "$RUN-mon" --network "$NET" --read-only --tmpfs /tmp \
        "${HOST_ARGS[@]}" "${args[@]}" "$IMAGE" >/dev/null
    CONTAINERS+=("$RUN-mon")
}

follow() {
    echo
    if [ "$DURATION" = "0" ]; then
        echo "== following logs, Ctrl-C to stop"
    else
        echo "== following logs for ${DURATION}s (Ctrl-C to stop early)"
    fi
    echo
    for spec in "$@"; do
        local name="${spec%%:*}" prefix="${spec#*:}"
        docker logs -f "$name" 2>&1 | sed -u "s/^/${prefix} /" &
    done
    if [ "$DURATION" = "0" ]; then
        wait
    else
        sleep "$DURATION"
    fi
}

case "$SCENARIO" in
log)
    need_image
    hostify
    docker network create "$NET" >/dev/null
    start_detector
    follow "$RUN-mon:[detector]"
    ;;
webhook)
    need_image
    hostify
    docker network create "$NET" >/dev/null
    cp "$(dirname "$0")/webhook_sink.py" "$WORK/"
    chmod 777 "$WORK"; : > "$HOOK_LOG"; chmod 666 "$HOOK_LOG"
    start_sink 200
    start_detector "http://$RUN-hook:9000/"
    follow "$RUN-mon:[detector]" "$RUN-hook:[webhook] "
    ;;
silence)
    need_image
    docker network create "$NET" >/dev/null
    cp "$(dirname "$0")/webhook_sink.py" "$WORK/"
    chmod 777 "$WORK"; : > "$HOOK_LOG"; chmod 666 "$HOOK_LOG"
    start_local_stream
    start_sink 200
    start_detector "http://$RUN-hook:9000/"
    follow "$RUN-mon:[detector]" "$RUN-hook:[webhook] "
    ;;
retry)
    need_image
    docker network create "$NET" >/dev/null
    cp "$(dirname "$0")/webhook_sink.py" "$WORK/"
    chmod 777 "$WORK"; : > "$HOOK_LOG"; chmod 666 "$HOOK_LOG"
    start_local_stream
    start_sink 500
    start_detector "http://$RUN-hook:9000/"
    follow "$RUN-mon:[detector]" "$RUN-hook:[webhook] "
    ;;
down)
    need_image
    docker network create "$NET" >/dev/null
    cp "$(dirname "$0")/webhook_sink.py" "$WORK/"
    chmod 777 "$WORK"; : > "$HOOK_LOG"; chmod 666 "$HOOK_LOG"
    start_sink 200
    URL="http://127.0.0.1:9/nothing-here.mp3"
    start_detector "http://$RUN-hook:9000/"
    follow "$RUN-mon:[detector]" "$RUN-hook:[webhook] "
    ;;
*)
    sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
