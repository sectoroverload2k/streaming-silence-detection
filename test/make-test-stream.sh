#!/usr/bin/env bash
# Builds a tone -> silence -> tone mp3 and, by default, serves it on localhost
# so test/run.sh has something to point at that is not a real station.
#
#   test/make-test-stream.sh              # generate + serve on :8123
#   test/make-test-stream.sh file OUT.mp3 # just write the file
#   test/make-test-stream.sh stop         # stop the server
#
# ffmpeg comes from the detector image, and the server is a container with the
# port published, so nothing has to be installed on the host.
#
# Environment:
#   IMAGE     image providing ffmpeg/python  (default silence-detect:local)
#   PORT      host port to serve on          (default 8123)
#   NAME      file name to serve             (default my-test-stream.mp3)
#   TONE      seconds of tone at each end    (default 4)
#   SILENCE   seconds of silence in between  (default 10)
#   FREQ      tone frequency in Hz           (default 440)
set -euo pipefail

IMAGE="${IMAGE:-silence-detect:local}"
PORT="${PORT:-8123}"
NAME="${NAME:-my-test-stream.mp3}"
TONE="${TONE:-4}"
SILENCE="${SILENCE:-10}"
FREQ="${FREQ:-440}"

CONTAINER=silence-test-source
SOURCE_DIR="${SOURCE_DIR:-${TMPDIR:-/tmp}/silence-test-source}"

need_image() {
    if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
        echo "== image $IMAGE not found, building"
        docker build -t "$IMAGE" "$(dirname "$0")/.."
    fi
}

# The image runs as uid 10001, so the output directory has to be writable by
# it rather than only by the invoking user.
generate() {
    local out="$1"
    local dir name
    dir="$(cd "$(dirname "$out")" && pwd)"
    name="$(basename "$out")"
    chmod 777 "$dir" 2>/dev/null || true
    echo "== generating ${TONE}s tone / ${SILENCE}s silence / ${TONE}s tone -> $dir/$name"
    docker run --rm -v "$dir:/data" --entrypoint ffmpeg "$IMAGE" \
        -hide_banner -loglevel error \
        -f lavfi -i "sine=f=$FREQ:r=44100:d=$TONE" \
        -f lavfi -i "anullsrc=r=44100:cl=stereo:d=$SILENCE" \
        -f lavfi -i "sine=f=$FREQ:r=44100:d=$TONE" \
        -filter_complex "[0:a][1:a][2:a]concat=n=3:v=0:a=1" \
        -ac 2 -c:a libmp3lame -y "/data/$name"
    test -s "$out"
}

case "${1:-serve}" in
file)
    need_image
    OUT="${2:-./$NAME}"
    mkdir -p "$(dirname "$OUT")"
    generate "$OUT"
    ;;
serve)
    need_image
    mkdir -p "$SOURCE_DIR"
    generate "$SOURCE_DIR/$NAME"
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    if ! docker run -d --name "$CONTAINER" -p "$PORT:8000" -v "$SOURCE_DIR:/data" \
        --entrypoint python3 "$IMAGE" -m http.server 8000 --directory /data >/dev/null
    then
        echo "could not publish port $PORT - is something already listening on it?" >&2
        echo "retry with a free port: PORT=9123 $0 serve" >&2
        exit 1
    fi
    echo "== serving http://localhost:$PORT/$NAME"
    echo
    echo "   test/run.sh log"
    echo "   test/run.sh webhook"
    echo
    echo "   stop with: $0 stop"
    ;;
stop)
    docker rm -f "$CONTAINER" >/dev/null 2>&1 \
        && echo "== stopped $CONTAINER" \
        || echo "== $CONTAINER was not running"
    ;;
*)
    sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
