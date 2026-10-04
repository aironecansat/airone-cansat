# AirOne ground station — headless API/pipeline container.
# Security posture:
#   * runs as an unprivileged user (airone, uid 10001), no shell login;
#   * NO secret is baked into the image — AIRONE_JWT_SECRET must be supplied
#     at run time (the launcher refuses to start without it);
#   * no build toolchain or GUI libraries in the runtime image;
#   * healthcheck hits the real public route /api/v1/status.
FROM python:3.12-slim AS base

LABEL maintainer="Team AirOne"
LABEL description="AirOne CanSat Ground Station & AI Scientific Analysis Platform"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    QT_QPA_PLATFORM=offscreen

# Unprivileged runtime account.
RUN groupadd --system --gid 10001 airone \
    && useradd --system --uid 10001 --gid airone --home-dir /app --shell /usr/sbin/nologin airone

WORKDIR /app

# Dependencies first for layer caching. The GUI extras (PyQt5, matplotlib) are
# not needed headless; install only the pinned runtime set.
COPY requirements.txt .
RUN grep -v -E '^(PyQt5|matplotlib)==' requirements.txt > /tmp/req-headless.txt \
    && pip install -r /tmp/req-headless.txt \
    && rm /tmp/req-headless.txt

# Application code (secrets, .env and data are excluded via .dockerignore).
COPY --chown=airone:airone . .

RUN mkdir -p logs data models \
    && chown -R airone:airone /app/logs /app/data /app/models

USER airone

# Inside the container the API must bind all interfaces to be reachable from
# the host; publish the port only on loopback (see docker-compose.yml) or put
# a TLS reverse proxy in front.
ENV AIRONE_API_HOST=0.0.0.0

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5000/api/v1/status', timeout=4).status == 200 else 1)"

CMD ["python3", "launcher.py"]
