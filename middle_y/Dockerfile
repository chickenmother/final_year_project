# Middle Y — Decision Proxy & Local Fallback Dockerfile
# Python 3.11-slim base image

FROM python:3.11-slim

WORKDIR /app

# Install tc (iproute2) so scripts/inject_latency.sh can inject netem latency.
RUN apt-get update \
    && apt-get install -y --no-install-recommends iproute2 \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first for efficient layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy shared libraries and this service's source.
COPY shared/ ./shared/
COPY middle_y/ ./middle_y/

ENV PYTHONPATH=/app
EXPOSE 8001

CMD ["uvicorn", "middle_y.main:app", "--host", "0.0.0.0", "--port", "8001"]