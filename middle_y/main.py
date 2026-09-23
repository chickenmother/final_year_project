"""Middle Y — Decision Proxy & Local Fallback (FastAPI + httpx).

The middle tier of the 3-tier vertical cloud gaming latency simulator.

Architecture (two-layer separation)
------------------------------------
**Layer 2 — Background Health Probe** (asyncio.Task, runs independently)
  Probes Cloud X's ``/health`` every 1 second and updates cached state
  variables ``_cloud_rtt_ms`` and ``_forward_enabled``.  Zero per-request
  overhead — the game path never pays the probe cost.

**Layer 1 — Request Handler** (thin, per-request)
  Receives ``GameInputRequest`` from End Node (Z), reads the cached
  ``_forward_enabled`` flag, and either forwards to Cloud X or executes
  locally.  No HTTP health calls on this path.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from shared.config import (
    CLOUD_X_HOST,
    CLOUD_X_PORT,
    MIDDLE_Y_FALLBACK_RETURN_MS,
    MIDDLE_Y_HYSTERESIS_HALF_MS,
    MIDDLE_Y_LABEL,
    MIDDLE_Y_MIN_SWITCHES,
    MIDDLE_Y_OFFLOAD_RTT_MS,
    MIDDLE_Y_PORT,
)
from shared.schemas import (
    GameInputRequest,
    GameStateRequest,
    GameStateResponse,
    TaskStatus,
    middle_y_node,
    new_id,
)

# Upstream Cloud X URL (built once from shared config).
CLOUD_X_URL = f"http://{CLOUD_X_HOST}:{CLOUD_X_PORT}"

# ============================================================
# Application setup
# ============================================================
app = FastAPI(
    title="Middle Y — Decision Proxy & Local Fallback",
    description="Edge gateway with proxy logic for the 3-tier cloud gaming simulator",
    version="0.2.0",
)

logger = logging.getLogger("middle_y")
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)

# Runtime-mutable offload threshold (overridable via PUT /threshold).
_offload_threshold_ms: float = float(MIDDLE_Y_OFFLOAD_RTT_MS)

# ============================================================
# Shared state (written by Layer 2, read by Layer 1)
# ============================================================
# (the shared HTTP client `_http_client` is declared in its own section below)

_cloud_rtt_ms: float = 0.0          # Y → X round-trip (background probe)
_zy_rtt_ms: float = 2.0             # Z → Y round-trip (estimated per request)
_forward_enabled: bool = False
_probe_task: asyncio.Task | None = None

# EWMA-smoothed total user-path RTT used for the hysteresis decision.
_ewma_total_ms: float = 0.0
# Most recent RTT End Z measured (drives the decision; also exposed to GUI).
_last_measured_rtt_ms: float = 0.0
# Consecutive-sample counters: a switch requires MIN_SWITCHES agreeing samples
# *outside* the hysteresis band (prevents stick/flap on a single spike).
_fallback_streak: int = 0
_forward_streak: int = 0

# ============================================================
# Shared HTTP client (avoids ~10-30 ms TCP/TLS setup per request).
# ============================================================
_http_client: httpx.AsyncClient | None = None


# ============================================================
# Layer 2 — Background Health Probe (asyncio.Task)
# ============================================================
async def _background_probe_loop() -> None:
    """Probe Cloud X's ``/health`` every 1 s and update cached state.

    This records only ``_cloud_rtt_ms`` (the Y↔X probe) used for display
    and the one-way yx contribution.  It does NOT write the EWMA decision
    signal: that is owned solely by ``execute_or_proxy`` (Layer 1) using
    End Z's real measured RTT, so there is a single consistent writer and
    the hysteresis band can never be hit by conflicting signals.
    """
    global _cloud_rtt_ms
    logger.info("Background health probe started — targeting %s/health", CLOUD_X_URL)

    while True:
        t0 = time.perf_counter()
        try:
            resp = await _http_client.get(f"{CLOUD_X_URL}/health")
            t1 = time.perf_counter()
            rtt = (t1 - t0) * 1000.0
            _cloud_rtt_ms = round(rtt, 1)
            logger.debug(
                "Probe: yx=%.1f ms, threshold=%.0f ms",
                _cloud_rtt_ms, _offload_threshold_ms,
            )
        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError):
            _cloud_rtt_ms = -1.0
            logger.warning("Cloud X health probe failed")
        except Exception:
            _cloud_rtt_ms = -1.0

        await asyncio.sleep(1.0)


@app.on_event("startup")
async def _start_probe() -> None:
    """Launch the background health probe on application startup."""
    global _probe_task, _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=3.0)
    if _probe_task is None:
        _probe_task = asyncio.create_task(_background_probe_loop())


@app.on_event("shutdown")
async def _stop_probe() -> None:
    """Cancel the background health probe on application shutdown."""
    global _probe_task, _http_client
    if _probe_task is not None:
        _probe_task.cancel()
        try:
            await _probe_task
        except asyncio.CancelledError:
            pass
        _probe_task = None
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None
        logger.info("Background health probe stopped.")

# ============================================================
# Layer 1 — Request Handler helpers
# ============================================================


def _local_execute(payload: GameInputRequest, rtt_ms: float) -> GameStateResponse:
    """Run the game-state computation locally at Middle Y.

    Simulates lightweight edge processing — advances the frame counter and
    stamps the state with this tier's label.
    """
    now_ms = int(time.time() * 1000)

    state = dict(payload.inputs)
    state.setdefault("frame", 0)
    state["frame"] += 1
    state["processed_at_tier"] = MIDDLE_Y_LABEL

    logger.info(
        "[%s] [%.1f] [%s] - [%s]",
        now_ms,
        rtt_ms,
        MIDDLE_Y_LABEL,
        TaskStatus.EXECUTED.value,
    )

    return GameStateResponse(
        request_id=payload.request_id,
        status=TaskStatus.EXECUTED,
        state=state,
        latency_ms=rtt_ms,
        executed_by=MIDDLE_Y_LABEL,
        timestamp_ms=now_ms,
    )


async def _forward_to_cloud_x(
    payload: GameInputRequest,
) -> GameStateResponse | None:
    """Offload the work to Cloud X and return its response.

    Returns ``None`` on timeout or 5xx so the caller falls back to local.
    """
    t0 = time.perf_counter()
    forwarded = GameStateRequest(
        origin_input_id=payload.request_id,
        forwarded_by=middle_y_node(),
        state=dict(payload.inputs),
        timestamp_ms=payload.timestamp_ms,
    )

    try:
        resp = await _http_client.post(
            f"{CLOUD_X_URL}/execute",
            json=forwarded.model_dump(),
        )
        t1 = time.perf_counter()
        rtt_ms = (t1 - t0) * 1000.0

        if resp.status_code >= 500:
            logger.warning("Cloud X returned 5xx (status=%d), fallback", resp.status_code)
            return None

        resp.raise_for_status()
        result = GameStateResponse.model_validate(resp.json())
        result.latency_ms = rtt_ms  # inject measured RTT
        return result

    except (httpx.TimeoutException, httpx.HTTPStatusError, httpx.RequestError) as exc:
        logger.warning("Cloud X forward failed: %s, falling back to local", exc)
        return None


# ============================================================
# Layer 1 — Request Handler (route)
# ============================================================
@app.get("/health", status_code=200)
async def health_check() -> dict:
    """Liveness probe for the edge gateway."""
    return {"status": "ok", "tier": MIDDLE_Y_LABEL}


@app.post(
    "/execute",
    response_model=GameStateResponse,
    status_code=200,
    responses={
        200: {"description": "Game state executed (forwarded or local)"},
        422: {"description": "Validation error in request payload"},
        500: {"description": "Internal proxy failure"},
    },
)
async def execute_or_proxy(payload: GameInputRequest) -> GameStateResponse:
    """Proxy decision entry-point — decides on the REAL user-path RTT.

    Uses the ground-truth ``measured_rtt_ms`` that End Z captures on its own
    request/response round-trip (sent inside ``inputs``).  That is exactly the
    number the user's latency chart shows, so the migration boundary always
    agrees with the chart.  Falls back to the local timestamp estimate only if
    End Z hasn't reported a measurement yet.

    A smoothed EWMA drives an ASYMMETRIC switch policy (the measured RTT is
    mode-dependent — forwarding includes the Y→X leg, local execution does
    not — so a symmetric band oscillates):
    - forward → fallback only if EWMA EXCEEDS (threshold + hyst) for
      ``MIDDLE_Y_MIN_SWITCHES`` consecutive samples (network degraded).
    - fallback → forward only if EWMA ≤ ``MIDDLE_Y_FALLBACK_RETURN_MS`` for
      ``MIDDLE_Y_MIN_SWITCHES`` consecutive samples (network near-clean).
    This keeps the migration stable: local-mode RTT (~20 ms) sits above the
    return threshold, so once on Y it stays there until delays are removed.
    """
    global _zy_rtt_ms, _ewma_total_ms, _forward_enabled
    global _fallback_streak, _forward_streak, _last_measured_rtt_ms

    now_ms = int(time.time() * 1000)

    # Prefer End Z's actual measured round-trip RTT (chart ground truth).
    measured = payload.inputs.get("measured_rtt_ms")
    if isinstance(measured, (int, float)) and measured > 0:
        total_rtt = float(measured)
        _zy_rtt_ms = round(total_rtt / 2.0, 1)
    else:
        # Fallback: rough Z↔Y estimate from the request timestamp + Y↔X probe.
        zy_one_way = max(1.0, float(now_ms - payload.timestamp_ms))
        _zy_rtt_ms = round(zy_one_way * 2.0, 1)
        yx_rtt = _cloud_rtt_ms if _cloud_rtt_ms > 0 else 2.0
        total_rtt = _zy_rtt_ms + yx_rtt

    # EWMA smoothing so a single reading can't flip/stick the decision.
    _last_measured_rtt_ms = round(total_rtt, 1)
    if _ewma_total_ms <= 0:
        _ewma_total_ms = float(total_rtt)
    else:
        _ewma_total_ms = 0.6 * _ewma_total_ms + 0.4 * float(total_rtt)

    # Asymmetric hysteresis (avoids mode-dependent oscillation):
    # - forward → fallback only when EWMA > threshold + hyst (network degraded).
    # - fallback → forward only when EWMA ≤ FALLBACK_RETURN_MS (near-clean).
    fall_threshold = _offload_threshold_ms + MIDDLE_Y_HYSTERESIS_HALF_MS

    # Debounce: switch only for MIN_SWITCHES consecutive agreeing samples.
    if _forward_enabled:
        # Currently forwarding — degrade only if RTT stays high.
        if _ewma_total_ms > fall_threshold:
            _fallback_streak += 1
            _forward_streak = 0
            if _fallback_streak >= MIDDLE_Y_MIN_SWITCHES:
                _forward_enabled = False
        else:
            _fallback_streak = 0
            _forward_streak += 1
    else:
        # Currently in local fallback — return to X only when near-clean.
        if _ewma_total_ms <= MIDDLE_Y_FALLBACK_RETURN_MS:
            _forward_streak += 1
            _fallback_streak = 0
            if _forward_streak >= MIDDLE_Y_MIN_SWITCHES:
                _forward_enabled = True
        else:
            _fallback_streak += 1
            _forward_streak = 0

    if _forward_enabled:
        logger.info(
            "Forwarding to Cloud X (total=%.1f ms, ewma=%.1f ms, threshold=%.0f ms)",
            total_rtt, _ewma_total_ms, _offload_threshold_ms,
        )
        forwarded = await _forward_to_cloud_x(payload)
        if forwarded is not None:
            return forwarded
        logger.info("Forward failed — falling back to local execution")
        return _local_execute(payload, total_rtt)

    logger.info(
        "Local fallback (total=%.1f ms, ewma=%.1f ms > band %.0f±%.0f): "
        "streak=%d/%d",
        total_rtt, _ewma_total_ms,
        _offload_threshold_ms, MIDDLE_Y_HYSTERESIS_HALF_MS,
        _fallback_streak, MIDDLE_Y_MIN_SWITCHES,
    )
    return _local_execute(payload, total_rtt)


# ---------------------------------------------------------------------------
# Threshold control (runtime override, persisted in memory only)
# ---------------------------------------------------------------------------
@app.get("/threshold", status_code=200)
async def get_threshold() -> dict:
    """Return the current offload threshold and the config default."""
    return {
        "threshold_ms": _offload_threshold_ms,
        "default_ms": MIDDLE_Y_OFFLOAD_RTT_MS,
        "tier": MIDDLE_Y_LABEL,
    }


@app.put("/threshold", status_code=200)
async def update_threshold(payload: dict) -> dict:
    """Override the offload RTT threshold at runtime."""
    global _offload_threshold_ms
    new_val = float(payload.get("threshold_ms", _offload_threshold_ms))
    _offload_threshold_ms = max(1.0, min(new_val, 2000.0))  # clamp 1..2000
    logger.info("Offload threshold updated to %.1f ms", _offload_threshold_ms)
    return {
        "threshold_ms": _offload_threshold_ms,
        "default_ms": MIDDLE_Y_OFFLOAD_RTT_MS,
    }


# ---------------------------------------------------------------------------
# State endpoint (live probe data for the GUI)
# ---------------------------------------------------------------------------
@app.get("/state", status_code=200)
async def get_state() -> dict:
    """Return the current probe state and forwarding decision."""
    return {
        "forward_enabled": _forward_enabled,
        "cloud_rtt_ms": _cloud_rtt_ms,
        "zy_rtt_ms": _zy_rtt_ms,
        "total_rtt_ms": _last_measured_rtt_ms,
        "measured_rtt_ms": _last_measured_rtt_ms,
        "ewma_total_ms": round(_ewma_total_ms, 1),
        "fallback_streak": _fallback_streak,
        "forward_streak": _forward_streak,
        "hysteresis_half_ms": MIDDLE_Y_HYSTERESIS_HALF_MS,
        "fallback_return_ms": MIDDLE_Y_FALLBACK_RETURN_MS,
        "min_switches": MIDDLE_Y_MIN_SWITCHES,
        "threshold_ms": _offload_threshold_ms,
        "tier": MIDDLE_Y_LABEL,
    }


# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------
@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all handler returning a structured error."""
    logger.exception("Unhandled error while processing %s", request.url)
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_server_error",
            "detail": str(exc),
            "request_id": new_id("err"),
            "raised_by": MIDDLE_Y_LABEL,
            "timestamp_ms": int(time.time() * 1000),
        },
    )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "middle_y.main:app",
        host="0.0.0.0",
        port=MIDDLE_Y_PORT,
        log_level="info",
    )