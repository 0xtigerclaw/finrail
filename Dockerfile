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
    FINRAIL_HOST=0.0.0.0

EXPOSE 8000
CMD ["finrail-console"]
