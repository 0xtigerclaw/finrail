FROM python:3.12-slim

WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir ".[console]"

# Demo deployment: state is the container's local disk. Runs, approvals and
# finance artefacts live there for the life of the instance; scale to a
# single warm instance for the demo so the ledger one judge reads is the
# ledger the demo wrote.
ENV FINRAIL_STATE=/data/state \
    FINRAIL_HOME=/data/home \
    FINRAIL_ENGINE=nebius \
    FINRAIL_HOST=0.0.0.0 \
    FINRAIL_DAILY_POST_CAP=300 \
    PYTHONUNBUFFERED=1

# The server runs as an unprivileged user that owns only its state directory.
RUN useradd --system --uid 1000 --create-home --home-dir /data finrail \
    && mkdir -p /data/state /data/home \
    && chown -R finrail:finrail /data
USER finrail

EXPOSE 8000
# Liveness only: the process answers and its state dir takes a write.
# Readiness (engine key, corpus) is GET /readyz; see docs/operations.md.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os, sys, urllib.request; port = os.environ.get('FINRAIL_PORT') or os.environ.get('PORT') or '8000'; sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=4).status == 200 else 1)"]
CMD ["finrail-console"]
