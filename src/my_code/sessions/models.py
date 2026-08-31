"""Session identity, metadata, and hydrated persistence state."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal
from uuid import UUID

from my_code.model.capabilities import ModelLimits
from my_code.model.primitives import TokenUsage

type InvocationOutcome = Literal["succeeded", "max_steps", "failed", "cancelled"]


class CollaborationMode(StrEnum):
    """User-selected interaction contract, independent of base permissions."""

    DEFAULT = "default"
    PLAN = "plan"


class SessionKind(StrEnum):
    """决定会话目录与恢复策略的持久化产品分类。"""

    FOREGROUND = "foreground"
    SUBAGENT = "subagent"


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
    session_kind: str = SessionKind.FOREGROUND.value
    parent_session_id: str | None = None
    created_by_run_id: str | None = None
    agent_name: str | None = None

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
        try:
            kind = SessionKind(self.session_kind)
        except ValueError as error:
            raise ValueError("Unsupported session kind") from error
        for name, value in (
            ("parent_session_id", self.parent_session_id),
            ("created_by_run_id", self.created_by_run_id),
        ):
            if value is not None:
                try:
                    parsed = UUID(value)
                except ValueError as error:
                    raise ValueError(f"{name} must be a UUID or null") from error
                if str(parsed) != value.lower():
                    raise ValueError(f"{name} must use canonical UUID syntax")
        if self.agent_name is not None and not self.agent_name.strip():
            raise ValueError("agent_name must be non-empty or null")
        lineage = (
            self.parent_session_id,
            self.created_by_run_id,
            self.agent_name,
        )
        if kind is SessionKind.FOREGROUND and any(item is not None for item in lineage):
            raise ValueError("Foreground sessions cannot declare child lineage")
        if kind is SessionKind.SUBAGENT and any(item is None for item in lineage):
            raise ValueError("Subagent sessions require complete child lineage")
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
class InvocationStarted:
    invocation_id: str
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
            ("invocation_id", self.invocation_id),
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
class InvocationFinished:
    invocation_id: str
    finished_at: str
    outcome: InvocationOutcome
    completed_steps: int | None = None
    max_steps: int | None = None
    usage: TokenUsage | None = None
    error_type: str | None = None

    def __post_init__(self) -> None:
        if not self.invocation_id.strip():
            raise ValueError("invocation_id must not be empty")
        _timestamp(self.finished_at, "finished_at")
        if self.outcome not in {"succeeded", "max_steps", "failed", "cancelled"}:
            raise ValueError("Unsupported invocation outcome")
        if self.completed_steps is not None and self.completed_steps < 0:
            raise ValueError("completed_steps must not be negative")
        if self.max_steps is not None and self.max_steps < 1:
            raise ValueError("max_steps must be positive or null")
        if self.error_type is not None and not self.error_type.strip():
            raise ValueError("error_type must be non-empty or null")
        if self.outcome == "succeeded":
            if self.completed_steps is None or self.usage is None:
                raise ValueError(
                    "Succeeded invocation requires completed_steps and usage"
                )
            if self.max_steps is not None or self.error_type is not None:
                raise ValueError("Succeeded invocation has incompatible fields")
        elif self.outcome == "max_steps":
            if (
                self.completed_steps is None
                or self.max_steps is None
                or self.usage is None
            ):
                raise ValueError(
                    "Max-steps invocation requires completed_steps, max_steps, "
                    "and usage"
                )
            if self.error_type is not None:
                raise ValueError("Max-steps invocation cannot include error_type")
        elif self.outcome == "failed":
            if self.error_type is None:
                raise ValueError("Failed invocation requires error_type")
            if self.max_steps is not None or self.usage is not None:
                raise ValueError("Failed invocation has incompatible fields")
        elif self.max_steps is not None or self.usage is not None or self.error_type:
            raise ValueError("Cancelled invocation has incompatible fields")


@dataclass(frozen=True, slots=True)
class InvocationHistoryEntry:
    started: InvocationStarted
    finished: InvocationFinished | None = None


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
    "SessionKind",
    "SessionStart",
    "InvocationFinished",
    "InvocationHistoryEntry",
    "InvocationOutcome",
    "InvocationStarted",
]
