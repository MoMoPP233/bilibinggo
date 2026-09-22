"""First-party dynamic identity resolution shared by all activity sources.

The following feed exposes repost wrappers while the activity store needs the
original dynamic as its stable identity.  This module deliberately only
interprets fields already present in first-party dynamic payloads; it never
performs a remote lookup.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.sources.common import is_valid_dynamic_id, opus_link


@dataclass(frozen=True)
class CanonicalIdentity:
    raw_dynamic_id: str
    canonical_dynamic_id: str | None
    wrapper_author_uid: str | None
    canonical_author_uid: str | None
    is_forward: bool
    following: bool | None = None
    canonical_item: dict[str, Any] | None = None
    unresolved_reason: str | None = None

    @property
    def source_url(self) -> str | None:
        if not self.canonical_dynamic_id:
            return None
        return opus_link(self.canonical_dynamic_id)

    @property
    def observed_wrapper_dynamic_id(self) -> str:
        return self.raw_dynamic_id


def _modules(item: dict[str, Any]) -> dict[str, Any]:
    modules = item.get("modules")
    if isinstance(modules, dict):
        return modules
    if isinstance(modules, list):
        merged: dict[str, Any] = {}
        for module in modules:
            if not isinstance(module, dict):
                continue
            for key, value in module.items():
                if key not in merged and isinstance(value, dict):
                    merged[key] = value
        return merged
    return {}


def _author(item: dict[str, Any]) -> dict[str, Any]:
    author = _modules(item).get("module_author")
    return author if isinstance(author, dict) else {}


def _uid(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("mid", "uid", "user_id"):
            candidate = value.get(key)
            if candidate is not None:
                value = candidate
                break
    if value is None:
        return None
    text = str(value).strip()
    return text if text.isdigit() and int(text) > 0 else None


def _dynamic_id(item: dict[str, Any]) -> str | None:
    for key in ("id_str", "dynamic_id", "id"):
        value = item.get(key)
        if value is None:
            continue
        candidate = str(value).strip()
        if is_valid_dynamic_id(candidate):
            return candidate
    return None


def _following(author: dict[str, Any], item: dict[str, Any]) -> bool | None:
    """Read only explicit relationship evidence; unknown stays unknown."""
    values: list[tuple[str, Any]] = []
    for source in (author, item):
        for key in ("following", "is_following", "followed"):
            if key in source:
                values.append((key, source.get(key)))
        relation = source.get("relation")
        if isinstance(relation, dict):
            for key in ("following", "is_following", "followed", "attribute"):
                if key in relation:
                    values.append((key, relation.get(key)))
    for key, value in values:
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            if key == "attribute" and value in (2, 6):
                return True
            if value in (0, 1):
                return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if key == "attribute" and normalized in {"2", "6"}:
                return True
            if normalized in {"true", "yes", "following", "followed", "1"}:
                return True
            if normalized in {"false", "no", "not_following", "unfollowed", "0"}:
                return False
    return None


def resolve_dynamic_identity(
    item: dict[str, Any] | None,
    raw_dynamic_id: str | None = None,
) -> CanonicalIdentity:
    """Resolve a dynamic wrapper to its original identity without I/O."""
    payload = item if isinstance(item, dict) else {}
    raw_id = str(raw_dynamic_id or _dynamic_id(payload) or "").strip()
    wrapper_author = _uid(_author(payload))
    item_type = str(payload.get("type") or "").strip()
    is_forward = item_type == "DYNAMIC_TYPE_FORWARD"
    if not is_forward:
        return CanonicalIdentity(
            raw_dynamic_id=raw_id,
            canonical_dynamic_id=raw_id if is_valid_dynamic_id(raw_id) else None,
            wrapper_author_uid=wrapper_author,
            canonical_author_uid=wrapper_author,
            is_forward=False,
            following=_following(_author(payload), payload),
            canonical_item=payload or None,
        )

    original = payload.get("orig")
    if not isinstance(original, dict):
        return CanonicalIdentity(
            raw_dynamic_id=raw_id,
            canonical_dynamic_id=None,
            wrapper_author_uid=wrapper_author,
            canonical_author_uid=None,
            is_forward=True,
            following=None,
            unresolved_reason="forward_identity_unresolved",
        )
    original_id = _dynamic_id(original)
    original_author = _uid(_author(original))
    if not original_id or not original_author:
        return CanonicalIdentity(
            raw_dynamic_id=raw_id,
            canonical_dynamic_id=None,
            wrapper_author_uid=wrapper_author,
            canonical_author_uid=original_author,
            is_forward=True,
            following=_following(_author(original), original),
            canonical_item=original,
            unresolved_reason="forward_identity_unresolved",
        )
    return CanonicalIdentity(
        raw_dynamic_id=raw_id,
        canonical_dynamic_id=original_id,
        wrapper_author_uid=wrapper_author,
        canonical_author_uid=original_author,
        is_forward=True,
        following=_following(_author(original), original),
        canonical_item=original,
    )
