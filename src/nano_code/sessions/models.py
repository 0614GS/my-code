"""Session identity, metadata, and hydrated persistence state."""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from uuid import UUID

from nano_code.conversation import (
    CompactBoundary,
    ContentReplacement,
    ConversationMessage,
)
from nano_code.model import ModelLimits
from nano_code.tools import ToolResultPresentation


@dataclass(frozen=True, slots=True)
class SessionStart:
    session_id: str
    created_at: str
    cwd: str
    provider_id: str
    model: str
    permission_mode: str
    max_steps: int | None
    max_output_tokens: int
    context_chars: int
    model_limits: ModelLimits = ModelLimits()
    model_limit_source: str | None = None
    compact_trigger_tokens: int | None = None

    def __post_init__(self) -> None:
        try:
            parsed_id = UUID(self.session_id)
        except ValueError as error:
            raise ValueError("session_id must be a UUID") from error
        if str(parsed_id) != self.session_id.lower():
            raise ValueError("session_id must use canonical UUID syntax")
        _timestamp(self.created_at, "created_at")
        if not Path(self.cwd).is_absolute():
            raise ValueError("cwd must be an absolute path")
        if not self.provider_id or not self.model or not self.permission_mode:
            raise ValueError("Session start strings must not be empty")
        if self.max_steps is not None and self.max_steps < 1:
            raise ValueError("max_steps must be positive or null")
        if self.max_output_tokens < 1 or self.context_chars < 1:
            raise ValueError("Session limits must be positive")
        if self.compact_trigger_tokens is not None and self.compact_trigger_tokens < 1:
            raise ValueError("Session compact trigger must be positive or null")


@dataclass(frozen=True, slots=True)
class SessionMetadata:
    created_at: str
    updated_at: str
    title: str | None = None
    last_prompt: str | None = None

    def __post_init__(self) -> None:
        created = _timestamp(self.created_at, "created_at")
        updated = _timestamp(self.updated_at, "updated_at")
        if updated < created:
            raise ValueError("updated_at cannot precede created_at")
        for name, value in (("title", self.title), ("last_prompt", self.last_prompt)):
            if value is not None and not value.strip():
                raise ValueError(f"{name} must be non-empty or null")


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    history: tuple[ConversationMessage, ...]
    working_set: tuple[ConversationMessage, ...]
    content_replacements: tuple[ContentReplacement, ...] = field(default_factory=tuple)
    compact_boundaries: tuple[CompactBoundary, ...] = field(default_factory=tuple)
    tool_presentations: tuple[tuple[str, ToolResultPresentation], ...] = field(
        default_factory=tuple
    )
    metadata: SessionMetadata | None = None


def _timestamp(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed


__all__ = ["SessionMetadata", "SessionSnapshot", "SessionStart"]
