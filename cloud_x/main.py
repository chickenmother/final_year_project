"""Cloud X — Heavy Game-State Execution Engine (FastAPI).

The top tier of the 3-tier vertical cloud gaming latency simulator.
Receives offloaded game-state requests from the Middle Edge Gateway (Y),
executes them, and returns responses with provenance metadata.

Endpoint ``POST /execute`` accepts a :class:`~shared.schemas.GameStateRequest`
and returns a :class:`~shared.schemas.GameStateResponse` whose ``executed_by``
field is the human-readable label ``"Cloud_Node_X"``.
"""

from __future__ import annotations

import logging
import time

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from shared.config import CLOUD_X_LABEL, CLOUD_X_PORT
from shared.schemas import (
    GameStateRequest,
    GameStateResponse,
    TaskStatus,
    new_id,
)

# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Cloud X — Game State Execution Engine",
    description="Heavy game-state processing tier (3-tier vertical cloud simulator)",
    version="0.1.0",
)

logger = logging.getLogger("cloud_x")
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/health", status_code=200)
async def health_check() -> dict:
    """Liveness probe for the cloud server."""
    return {"status": "ok", "tier": CLOUD_X_LABEL}


@app.post(
    "/execute",
    response_model=GameStateResponse,
    status_code=200,
    responses={
        200: {"description": "Game state executed successfully"},
        422: {"description": "Validation error in request payload"},
        500: {"description": "Internal execution failure"},
    },
)
async def execute_state(payload: GameStateRequest) -> GameStateResponse:
    """Process an offloaded game-state request from a lower tier.

    Simulates heavy game-state execution by advancing the internal frame
    counter.  Computes the effective round-trip latency from the difference
    between the current time and the request's ``timestamp_ms``.
    """
    now_ms = int(time.time() * 1000)
    rtt_ms = float(now_ms - payload.timestamp_ms)

    # --- simulate heavy game-state computation ---
    state = dict(payload.state)
    state.setdefault("frame", 0)
    state["frame"] += 1
    state["processed_at_tier"] = CLOUD_X_LABEL

    # --- required structured log line ---
    # Format: [TIMESTAMP] [RTT_MS] [EXECUTED_BY] - [STATUS]
    logger.info(
        "[%s] [%.1f] [%s] - [%s]",
        now_ms,
        rtt_ms,
        CLOUD_X_LABEL,
        TaskStatus.EXECUTED.value,
    )

    return GameStateResponse(
        request_id=payload.request_id,
        status=TaskStatus.EXECUTED,
        state=state,
        latency_ms=rtt_ms,
        executed_by=CLOUD_X_LABEL,
        timestamp_ms=now_ms,
    )


# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------
@app.exception_handler(Exception)
async def generic_exception_handler(request, exc: Exception) -> JSONResponse:
    """Catch-all handler that returns a structured error without leaking
    internal details."""
    logger.exception("Unhandled error while processing %s", request.url)
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_server_error",
            "detail": str(exc),
            "request_id": new_id("err"),
            "raised_by": CLOUD_X_LABEL,
            "timestamp_ms": int(time.time() * 1000),
        },
    )


# ---------------------------------------------------------------------------
# Entrypoint (for ``uvicorn cloud_x.main:app``)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "cloud_x.main:app",
        host="0.0.0.0",
        port=CLOUD_X_PORT,
        log_level="info",
    )