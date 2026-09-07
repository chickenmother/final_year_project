# Cloud X — Heavy Game-State Execution Engine Dockerfile
# Python 3.11-slim base image (AGENT.md convention)

FROM python:3.11-slim

WORKDIR /app

# Install tc (iproute2) so scripts/inject_latency.sh can inject netem latency.
RUN apt-get update \
    && apt-get install -y --no-install-recommends iproute2 \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first so layer caching works efficiently.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy shared libraries and this service's source.
COPY shared/ ./shared/
COPY cloud_x/ ./cloud_x/

ENV PYTHONPATH=/app
EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --retries=2 CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "cloud_x.main:app", "--host", "0.0.0.0", "--port", "8000"]