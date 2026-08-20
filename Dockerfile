# Alpine + ffmpeg + python3 (stdlib only). Multi-arch: both linux/amd64 and
# linux/arm64 come from the same Dockerfile with no per-arch branching, since
# nothing here is compiled.
FROM alpine:3.22

RUN apk add --no-cache ffmpeg python3 \
    && adduser -D -u 10001 silence

COPY silence_monitor.py /usr/local/bin/silence-monitor
COPY healthcheck.sh /usr/local/bin/healthcheck

RUN chmod +x /usr/local/bin/silence-monitor /usr/local/bin/healthcheck

USER 10001

ENV SILENCE_DURATION=5 \
    SILENCE_THRESHOLD=-50dB \
    HEARTBEAT_FILE=/tmp/heartbeat \
    HEARTBEAT_MAX_AGE=30 \
    PYTHONUNBUFFERED=1

# Liveness = ffmpeg is still decoding audio. The monitor rewrites the heartbeat
# file from ffmpeg's -progress output, which stops advancing when the stream
# stalls, even if the process is still alive.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["/usr/local/bin/healthcheck"]

ENTRYPOINT ["/usr/local/bin/silence-monitor"]
