#!/bin/sh
# Fails when the heartbeat file has not been rewritten for HEARTBEAT_MAX_AGE
# seconds, i.e. ffmpeg has stopped making progress on the stream.
set -eu

FILE="${HEARTBEAT_FILE:-/tmp/heartbeat}"
MAX_AGE="${HEARTBEAT_MAX_AGE:-30}"

[ -f "$FILE" ] || { echo "no heartbeat file at $FILE"; exit 1; }

AGE=$(( $(date +%s) - $(stat -c %Y "$FILE") ))
if [ "$AGE" -gt "$MAX_AGE" ]; then
    echo "heartbeat is ${AGE}s old (max ${MAX_AGE}s)"
    exit 1
fi
echo "ok, heartbeat ${AGE}s old"
