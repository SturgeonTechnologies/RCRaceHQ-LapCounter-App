# Lap counter app image: one image, several services (web, lapd, syncd; later
# announcerd), chosen by the command in compose.yaml. Built for linux/arm64 (Pi 4).
FROM python:3.12-slim-bookworm AS build
WORKDIR /src
COPY pyproject.toml ./
COPY lapcounter ./lapcounter
RUN pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.12-slim-bookworm
# Shown on the public GHCR package page and by `docker inspect`.
LABEL org.opencontainers.image.title="RC Race HQ Lap Counter" \
      org.opencontainers.image.description="Lap counter app for Raspberry Pi: live timing, heats, check-in kiosk, transponder learning" \
      org.opencontainers.image.vendor="Sturgeon Technologies LLC" \
      org.opencontainers.image.licenses="FSL-1.1-ALv2" \
      org.opencontainers.image.url="https://www.rcracehq.com" \
      org.opencontainers.image.source="https://github.com/SturgeonTechnologies/RCRaceHQ-LapCounter-App"
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LAPCOUNTER_DB=/var/lib/lapcounter/lapcounter.db \
    OPENSTINT_ZMQ=tcp://127.0.0.1:5556
RUN --mount=type=bind,from=build,source=/wheels,target=/wheels \
    pip install --no-cache-dir --no-index --find-links=/wheels lapcounter \
 && useradd --system --uid 10001 --home-dir /var/lib/lapcounter --shell /usr/sbin/nologin lapcounter \
 && install -d -o lapcounter -g lapcounter -m 0750 /var/lib/lapcounter
USER lapcounter
VOLUME ["/var/lib/lapcounter"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)" || exit 1
# gthread: the live view is a long-lived SSE stream per browser tab.
CMD ["gunicorn", "--worker-class", "gthread", "--workers", "2", "--threads", "16", \
     "--bind", "0.0.0.0:8080", "--access-logfile", "-", "lapcounter.web:create_app()"]
