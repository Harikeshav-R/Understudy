"""Twin environment contract models."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class TwinHandle(BaseModel):
    """Handle to an isolated twin environment."""

    model_config = ConfigDict(frozen=True)

    twin_id: str
    incident_id: str
    candidate_index: int
    namespace: str
    database: str
    forked_from_snapshot_at: datetime
    ready_at: datetime | None = None
    state: Literal["forking", "ready", "applied", "observing", "torn_down", "failed"]


class MirrorStats(BaseModel):
    """Traffic mirroring statistics for a twin."""

    model_config = ConfigDict(frozen=True)

    twin_id: str
    delivered: int = 0
    dropped: int = 0

    @property
    def drop_ratio(self) -> float:
        """Calculate the ratio of dropped mirrored requests to total requests."""
        total = self.delivered + self.dropped
        return (self.dropped / total) if total > 0 else 0.0
