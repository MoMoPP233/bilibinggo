from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class RepostCleanupDeleteRequest(BaseModel):
    """Explicitly confirmed, bounded batch of the user's own repost IDs."""

    model_config = ConfigDict(extra="forbid")

    repost_dynamic_ids: list[str] = Field(min_length=1, max_length=20)
    confirmed: bool = False
    manual_review_confirmed: bool = False


class RepostDeferRequest(BaseModel):
    """Per-repost 用户暂不删除/恢复请求（本地操作，0 远程）。"""

    model_config = ConfigDict(extra="forbid")

    repost_dynamic_ids: list[str] = Field(min_length=1, max_length=100)
