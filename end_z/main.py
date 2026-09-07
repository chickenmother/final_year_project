"""End Z — Input Sampler & Latency Monitor (FastAPI + httpx).

The bottom tier of the 3-tier vertical cloud gaming latency simulator.
Sends continuous sample ``GameInputRequest`` payloads to the Middle Edge
Gateway (Y), measures round-trip times, classifies latency into bands, and
exposes the last N readings through a ``/metrics`` endpoint.

Background sampling loop
------------------------
- Runs every ``HEARTBEAT_INTERVAL_S`` (1.0 s) as an ``asyncio.Task``.
- Builds a synthetic game-input payload, POSTs it to Middle Y's ``/execute``.
- Logs each sample using the canonical format.
- Stores the last ``LATENCY_SAMPLE_COUNT`` (10) readings in a rotating deque.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from shared.config import (
    END_Z_LABEL,
    END_Z_PORT,
    HEARTBEAT_INTERVAL_S,
    LATENCY_SAMPLE_COUNT,
    LATENCY_DEGRADE_MS,
    LATENCY_GOOD_MS,
    LATENCY_IDEAL_MS,
    LATENCY_MIGRATE_MS,
    MIDDLE_Y_HOST,
    MIDDLE_Y_PORT,
)
from shared.schemas import (
    GameInputRequest,
    GameStateResponse,
    LatencyBand,
    LatencyReport,
    end_z_node,
    new_id,
)

# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
app = FastAPI(
    title="End Z — Input Sampler & Latency Monitor",
    description="Continuous latency sampling node (3-tier vertical cloud simulator)",
    version="0.1.0",
)

logger = logging.getLogger("end_z")
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)

MIDDLE_Y_URL = f"http://{MIDDLE_Y_HOST}:{MIDDLE_Y_PORT}"

# One shared HTTP client (avoids ~10-30ms TCP/TLS setup per request).
_http_client: httpx.AsyncClient | None = None

# Most recent RTT End Z actually measured (sent to Middle Y as ground truth).
_last_measured_rtt_ms: float = 0.0

# Thread-safe shared state for the background sampler.
_sampler_task: asyncio.Task | None = None
_metrics_deque: deque[LatencyReport] = deque(maxlen=LATENCY_SAMPLE_COUNT)
_metrics_lock = asyncio.Lock()
_sample_counter = 0

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _classify_band(rtt_ms: float) -> LatencyBand:
    """Map a measured RTT to a human-readable latency band."""
    if rtt_ms <= LATENCY_IDEAL_MS:
        return LatencyBand.IDEAL
    if rtt_ms <= LATENCY_GOOD_MS:
        return LatencyBand.GOOD
    if rtt_ms <= LATENCY_DEGRADE_MS:
        return LatencyBand.DEGRADED
    if rtt_ms <= LATENCY_MIGRATE_MS:
        return LatencyBand.MIGRATE
    return LatencyBand.LOCAL_FALLBACK


def _build_input_payload(frame: int) -> GameInputRequest:
    """Build a synthetic game-input payload for the next sample."""
    global _last_measured_rtt_ms
    return GameInputRequest(
        source=end_z_node(),
        inputs={
            "action": "sample",
            "frame": frame,
            "input": "move_right",
            # Ground-truth RTT measured by End Z on the previous sample.
            # Middle Y uses this (not a synthetic estimate) for its decision.
            "measured_rtt_ms": _last_measured_rtt_ms,
        },
        timestamp_ms=int(time.time() * 1000),
    )


async def _send_sample(frame: int) -> None:
    """Send one sample to Middle Y, measure RTT, log, and store metrics."""
    global _sample_counter, _last_measured_rtt_ms
    _sample_counter += 1

    payload = _build_input_payload(frame)
    t0 = time.perf_counter()

    try:
        resp = await _http_client.post(
            f"{MIDDLE_Y_URL}/execute",
            json=payload.model_dump(),
        )
        t1 = time.perf_counter()
        rtt_ms = (t1 - t0) * 1000.0

        result = GameStateResponse.model_validate(resp.json())
        band = _classify_band(rtt_ms)
        _last_measured_rtt_ms = round(rtt_ms, 1)

        # --- canonical log line ---
        logger.info(
            "[%s] [%.1f] [%s] - [%s]",
            payload.timestamp_ms,
            rtt_ms,
            END_Z_LABEL,
            "sampled",
        )

    except (httpx.TimeoutException, httpx.ConnectError, httpx.RequestError):
        # Middle Y unreachable — log a failure marker.
        rtt_ms = -1.0
        band = LatencyBand.LOCAL_FALLBACK
        logger.warning(
            "[%s] [---] [%s] - [%s]",
            payload.timestamp_ms,
            END_Z_LABEL,
            "failed",
        )

    # Store the reading.
    report = LatencyReport(rtt_ms=rtt_ms, band=band)
    async with _metrics_lock:
        _metrics_deque.append(report)


async def _sampling_loop() -> None:
    """Run forever, sending a sample every ``HEARTBEAT_INTERVAL_S`` seconds."""
    logger.info("Sampling loop started — probing %s/execute every %.1f s",
                MIDDLE_Y_URL, HEARTBEAT_INTERVAL_S)
    frame = 0
    while True:
        await _send_sample(frame)
        frame += 1
        await asyncio.sleep(HEARTBEAT_INTERVAL_S)
# ---------------------------------------------------------------------------
# Application lifecycle
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def _start_sampler() -> None:
    """Launch the background sampling loop on application startup."""
    global _sampler_task, _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=3.0)
    if _sampler_task is None:
        _sampler_task = asyncio.create_task(_sampling_loop())


@app.on_event("shutdown")
async def _stop_sampler() -> None:
    """Cancel the background sampling loop on application shutdown."""
    global _sampler_task, _http_client
    if _sampler_task is not None:
        _sampler_task.cancel()
        try:
            await _sampler_task
        except asyncio.CancelledError:
            pass
        _sampler_task = None
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None
        logger.info("Sampling loop stopped.")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/health", status_code=200)
async def health_check() -> dict:
    """Liveness probe for the end node."""
    return {
        "status": "ok",
        "tier": END_Z_LABEL,
        "running": _sampler_task is not None and not _sampler_task.done(),
        "samples_collected": _sample_counter,
    }


@app.get("/metrics", status_code=200)
async def get_metrics() -> dict:
    """Return the last ``LATENCY_SAMPLE_COUNT`` latency readings."""
    async with _metrics_lock:
        samples = list(_metrics_deque)
    return {
        "tier": END_Z_LABEL,
        "samples": [s.model_dump() for s in samples],
        "count": len(samples),
        "max_samples": LATENCY_SAMPLE_COUNT,
        "total_sent": _sample_counter,
    }


# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------
@app.exception_handler(Exception)
async def generic_exception_handler(request, exc: Exception) -> JSONResponse:
    """Catch-all handler returning a structured error."""
    logger.exception("Unhandled error while processing %s", request.url)
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_server_error",
            "detail": str(exc),
            "request_id": new_id("err"),
            "raised_by": END_Z_LABEL,
            "timestamp_ms": int(time.time() * 1000),
        },
    )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "end_z.main:app",
        host="0.0.0.0",
        port=END_Z_PORT,
        log_level="info",
    )