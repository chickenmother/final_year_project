"""Static configuration for the 3-tier vertical cloud gaming latency simulator.

Tier topology (data flow)::

    End Node (Z)  ---->  Middle Edge Gateway (Y)  ---->  Cloud Server (X)

Tasks migrate "up" the tiers (Z -> Y -> X) as network latency degrades, and
fall back "down" (X -> Y -> Z) when conditions recover.

Everything here is a plain constant so that all three FastAPI services and any
utility scripts can import the same source of truth without a framework.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Static node IPs (docker-compose subnet: 172.20.0.0/16)
# ---------------------------------------------------------------------------
CLOUD_X_HOST = "172.20.0.10"   # Tier X - cloud server (heavy game-state engine)
MIDDLE_Y_HOST = "172.20.0.20"  # Tier Y - edge gateway (decision proxy / fallback)
END_Z_HOST = "172.20.0.30"     # Tier Z - end node (input sampler / latency monitor)

# Stable logical identifiers used for provenance metadata (``executed_by``).
CLOUD_X_NODE_ID = "cloud_x"
MIDDLE_Y_NODE_ID = "middle_y"
END_Z_NODE_ID = "end_z"

# Human-readable labels used in logs and ``executed_by`` string fields.
CLOUD_X_LABEL = "Cloud_Node_X"
MIDDLE_Y_LABEL = "Middle_Node_Y"
END_Z_LABEL = "End_Node_Z"

# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------
CLOUD_X_PORT = 8000
MIDDLE_Y_PORT = 8001
END_Z_PORT = 8002

# ---------------------------------------------------------------------------
# Latency thresholds (milliseconds)
#
# These drive the dynamic migration decision: as the measured round-trip time
# crosses each band, work is pushed one tier further "up" (toward the cloud)
# or pulled back "down" (toward the edge / end node).
# ---------------------------------------------------------------------------
LATENCY_IDEAL_MS = 20        # Optimal cloud-gaming round-trip time.
LATENCY_GOOD_MS = 50         # Acceptable; no action required.
LATENCY_DEGRADE_MS = 100     # Degradation onset; consider migrating.
LATENCY_MIGRATE_MS = 200     # Hard threshold; migrate the task up a tier.
LATENCY_LOCAL_FALLBACK_MS = 300  # Cloud unusable; execute locally at Y/Z.

# ---------------------------------------------------------------------------
# Tier-specific offload thresholds (milliseconds)
# ---------------------------------------------------------------------------
MIDDLE_Y_OFFLOAD_RTT_MS = 40  # Middle Y forwards to Cloud X if RTT <= this.
END_Z_OFFLOAD_RTT_MS = 40     # End Z forwards to Middle Y if RTT <= this.

# Middle Y switch Hysteresis / debounce:
# - switch forward->fallback only after the smoothed total RTT exceeds
#   (threshold + HALF_HYSTERESIS) for MIN_SWITCHES consecutive samples.
# - switch fallback->forward only after 3 consecutive samples at or below
#   (threshold - HALF_HYSTERESIS).
MIDDLE_Y_HYSTERESIS_HALF_MS = 15.0
MIDDLE_Y_MIN_SWITCHES = 3

# ---------------------------------------------------------------------------
# Timing / sampling
# ---------------------------------------------------------------------------
HEARTBEAT_INTERVAL_S = 1.0   # Seconds between liveness/latency probes.
REQUEST_TIMEOUT_S = 2.0      # Max seconds to wait for an upstream response.
LATENCY_SAMPLE_WINDOW_S = 5.0  # Rolling window over which latency is averaged.
LATENCY_SAMPLE_COUNT = 120  # Samples retained in the End Z metrics sliding window (2 min at 1 Hz).
