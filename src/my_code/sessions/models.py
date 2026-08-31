"""Session identity, metadata, and hydrated persistence state."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal
from uuid import UUID

from my_code.model.capabilities import ModelLimits
from my_code.model.primitives import TokenUsage

type TurnOutcome = Literal["succeeded", "max_steps", "failed", "cancelled"]


class CollaborationMode(StrEnum):
    """User-selected interaction contract, independent of base permissions."""

    DEFAULT = "default"
    PLAN = "plan"


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
    model_limits: ModelLimits = ModelLimits()
    model_limit_source: str | None = None
    compact_trigger_tokens: int | None = None
    provider_protocol: str | None = None
    collaboration_mode: str = CollaborationMode.DEFAULT.value

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
        try:
            CollaborationMode(self.collaboration_mode)
        except ValueError as error:
            raise ValueError("Unsupported collaboration mode") from error
        if self.provider_protocol is not None and not self.provider_protocol.strip():
            raise ValueError("provider_protocol must be non-empty or null")
        if self.max_steps is not None and self.max_steps < 1:
            raise ValueError("max_steps must be positive or null")
        if self.max_output_tokens < 1:
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
class TurnStarted:
    turn_id: str
    run_id: str
    parent_run_id: str | None
    agent_name: str
    started_at: str
    continuation: bool
    evaluation_run_id: str | None = None
    test_case_id: str | None = None
    attempt_id: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("turn_id", self.turn_id),
            ("run_id", self.run_id),
            ("agent_name", self.agent_name),
        ):
            if not value.strip():
                raise ValueError(f"{name} must not be empty")
        _timestamp(self.started_at, "started_at")
        for name, value in (
            ("parent_run_id", self.parent_run_id),
            ("evaluation_run_id", self.evaluation_run_id),
            ("test_case_id", self.test_case_id),
            ("attempt_id", self.attempt_id),
        ):
            if value is not None and not value.strip():
                raise ValueError(f"{name} must be non-empty or null")


@dataclass(frozen=True, slots=True)
class TurnFinished:
    turn_id: str
    finished_at: str
    outcome: TurnOutcome
    completed_steps: int | None = None
    max_steps: int | None = None
    usage: TokenUsage | None = None
    error_type: str | None = None

    def __post_init__(self) -> None:
        if not self.turn_id.strip():
            raise ValueError("turn_id must not be empty")
        _timestamp(self.finished_at, "finished_at")
        if self.outcome not in {"succeeded", "max_steps", "failed", "cancelled"}:
            raise ValueError("Unsupported turn outcome")
        if self.completed_steps is not None and self.completed_steps < 0:
            raise ValueError("completed_steps must not be negative")
        if self.max_steps is not None and self.max_steps < 1:
            raise ValueError("max_steps must be positive or null")
        if self.error_type is not None and not self.error_type.strip():
            raise ValueError("error_type must be non-empty or null")
        if self.outcome == "succeeded":
            if self.completed_steps is None or self.usage is None:
                raise ValueError("Succeeded turn requires completed_steps and usage")
            if self.max_steps is not None or self.error_type is not None:
                raise ValueError("Succeeded turn has incompatible fields")
        elif self.outcome == "max_steps":
            if (
                self.completed_steps is None
                or self.max_steps is None
                or self.usage is None
            ):
                raise ValueError(
                    "Max-steps turn requires completed_steps, max_steps, and usage"
                )
            if self.error_type is not None:
                raise ValueError("Max-steps turn cannot include error_type")
        elif self.outcome == "failed":
            if self.error_type is None:
                raise ValueError("Failed turn requires error_type")
            if self.max_steps is not None or self.usage is not None:
                raise ValueError("Failed turn has incompatible fields")
        elif self.max_steps is not None or self.usage is not None or self.error_type:
            raise ValueError("Cancelled turn has incompatible fields")


@dataclass(frozen=True, slots=True)
class TurnHistoryEntry:
    started: TurnStarted
    finished: TurnFinished | None = None


def _timestamp(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed


__all__ = [
    "CollaborationMode",
    "SessionMetadata",
    "SessionStart",
    "TurnFinished",
    "TurnHistoryEntry",
    "TurnOutcome",
    "TurnStarted",
]
