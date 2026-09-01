from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class RepostCleanupDeleteRequest(BaseModel):
    """Explicitly confirmed, bounded batch of the user's own repost IDs."""

    model_config = ConfigDict(extra="forbid")

    repost_dynamic_ids: list[str] = Field(min_length=1, max_length=100)
    confirmed: bool = False
