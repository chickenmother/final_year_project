"""Shared configuration and Pydantic schemas used by all three tiers.

Re-export the most commonly used names so services can write ``from shared
import config`` or ``from shared.schemas import GameStateResponse`` directly.
"""

from shared import config, schemas  # noqa: F401

__all__ = ["config", "schemas"]
