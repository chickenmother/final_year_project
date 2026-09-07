"""Pydantic v2 request/response schemas shared across all three tiers.

These models define the REST payload format exchanged between::

    End Node (Z)  <->  Middle Edge Gateway (Y)  <->  Cloud Server (X)

Every response that represents executed work carries an ``executed_by``
:class:`NodeInfo` field so the system can attribute which tier actually ran
the task and observe the migration path over time.
"""

from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from shared import config


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class Tier(str, Enum):
    """The three vertical cloud tiers."""

    END_Z = "end_z"        # End Node (Z)
    MIDDLE_Y = "middle_y"  # Middle Edge Gateway (Y)
    CLOUD_X = "cloud_x"    # Cloud Server (X)


class TaskStatus(str, Enum):
    """Lifecycle state of a game-state task."""

    PENDING = "pending"
    EXECUTED = "executed"
    OFFLOADED = "offloaded"
    FAILED = "failed"


class LatencyBand(str, Enum):
    """Human-readable classification of the current network latency."""

    IDEAL = "ideal"
    GOOD = "good"
    DEGRADED = "degraded"
    MIGRATE = "migrate"
    LOCAL_FALLBACK = "local_fallback"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def utc_now() -> str:
    """Return the current UTC timestamp as an ISO-8601 string."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_id(prefix: str = "task") -> str:
    """Generate a short unique identifier for a request/task."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# Base model
# ---------------------------------------------------------------------------
class ApiModel(BaseModel):
    """Base model shared by every request/response payload."""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )


# ---------------------------------------------------------------------------
# Node identity / provenance
# ---------------------------------------------------------------------------
class NodeInfo(ApiModel):
    """Identity of a node in the tier topology.

    Used both to describe a request's origin and to record ``executed_by``
    provenance metadata on responses.
    """

    tier: Tier
    node_id: str = Field(..., description="Stable logical name, e.g. 'cloud_x'")
    host: str = Field(..., description="Static IP address (172.20.0.0/16 subnet)")
    port: int = Field(..., ge=1, le=65535)

    @field_validator("host")
    @classmethod
    def _host_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("host must not be blank")
        return v


def cloud_x_node() -> NodeInfo:
    """Build the standard identity for the Cloud Server (X)."""
    return NodeInfo(
        tier=Tier.CLOUD_X,
        node_id=config.CLOUD_X_NODE_ID,
        host=config.CLOUD_X_HOST,
        port=config.CLOUD_X_PORT,
    )


def middle_y_node() -> NodeInfo:
    """Build the standard identity for the Middle Edge Gateway (Y)."""
    return NodeInfo(
        tier=Tier.MIDDLE_Y,
        node_id=config.MIDDLE_Y_NODE_ID,
        host=config.MIDDLE_Y_HOST,
        port=config.MIDDLE_Y_PORT,
    )


def end_z_node() -> NodeInfo:
    """Build the standard identity for the End Node (Z)."""
    return NodeInfo(
        tier=Tier.END_Z,
        node_id=config.END_Z_NODE_ID,
        host=config.END_Z_HOST,
        port=config.END_Z_PORT,
    )


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------
class GameInputRequest(ApiModel):
    """Payload sent from the End Node (Z) to the Middle Gateway (Y).

    Carries the player's input for a single simulation step.
    """

    request_id: str = Field(default_factory=lambda: new_id("input"))
    source: NodeInfo
    inputs: dict[str, Any] = Field(..., description="Key/value player input events")
    timestamp_ms: int = Field(default_factory=lambda: int(time.time() * 1000))

    @field_validator("timestamp_ms")
    @classmethod
    def _ts_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("timestamp_ms must be positive")
        return v


class GameStateRequest(ApiModel):
    """Offload request forwarded up a tier (Y -> X).

    ``forwarded_by`` records which tier handed the task off so the migration
    chain remains traceable.
    """

    request_id: str = Field(default_factory=lambda: new_id("state"))
    origin_input_id: str
    forwarded_by: NodeInfo
    state: dict[str, Any] = Field(default_factory=dict)
    timestamp_ms: int = Field(default_factory=lambda: int(time.time() * 1000))


class HeartbeatRequest(ApiModel):
    """Periodic liveness signal with a one-way latency sample."""

    source: NodeInfo
    sent_at_ms: int = Field(default_factory=lambda: int(time.time() * 1000))


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------
class LatencyReport(ApiModel):
    """A single latency measurement observation."""

    rtt_ms: float = Field(..., ge=0.0)
    observed_at_ms: int = Field(default_factory=lambda: int(time.time() * 1000))
    band: LatencyBand = Field(default=LatencyBand.GOOD)


class GameStateResponse(ApiModel):
    """Result of a game-state task execution.

    ``executed_by`` is the provenance metadata identifying which tier actually
    ran the work.
    """

    request_id: str
    status: TaskStatus = Field(default=TaskStatus.EXECUTED)
    state: dict[str, Any] = Field(default_factory=dict)
    latency_ms: float = Field(..., ge=0.0, description="Observed round-trip latency")
    executed_by: str = Field(..., description="Human-readable node label, e.g. 'Cloud_Node_X'")
    timestamp_ms: int = Field(default_factory=lambda: int(time.time() * 1000))


class HeartbeatResponse(ApiModel):
    """Reply to a heartbeat, including the computed round-trip latency."""

    rtt_ms: float = Field(..., ge=0.0)
    responded_by: NodeInfo
    timestamp_ms: int = Field(default_factory=lambda: int(time.time() * 1000))


class ErrorResponse(ApiModel):
    """Uniform error payload for non-2xx responses."""

    error: str
    detail: str | None = None
    request_id: str | None = None
    raised_by: NodeInfo | None = None
    timestamp_ms: int = Field(default_factory=lambda: int(time.time() * 1000))

