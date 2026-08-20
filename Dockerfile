# Alpine + ffmpeg + python3 (stdlib only). Multi-arch: both linux/amd64 and
# linux/arm64 come from the same Dockerfile with no per-arch branching, since
# nothing here is compiled.
FROM alpine:3.22

LABEL org.opencontainers.image.title="streaming-silence-detection" \
      org.opencontainers.image.description="Icecast stream silence detection with webhook alerts" \
      org.opencontainers.image.licenses="Elastic-2.0" \
      org.opencontainers.image.source="https://github.com/sectoroverload2k/streaming-silence-detection"

RUN apk add --no-cache ffmpeg python3 \
    && adduser -D -u 10001 silence

COPY silence_monitor.py /usr/local/bin/silence-monitor
COPY healthcheck.sh /usr/local/bin/healthcheck

# ELv2 requires that anyone who receives a copy also receives the terms, and
# NOTICE records the licences of the ffmpeg/python/alpine bits bundled here.
COPY LICENSE NOTICE /usr/local/share/streaming-silence-detection/

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
