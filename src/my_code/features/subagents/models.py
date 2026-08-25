"""Foreground/background-neutral Subagent product values."""

from dataclasses import dataclass

from my_code.agent.models import AgentTurnOutcome
from my_code.conversation.attachments import AttachmentPayload
from my_code.tasks.models import TaskSnapshot


@dataclass(frozen=True, slots=True)
class SubagentLimits:
    max_depth: int = 3
    max_active_children: int = 4
    max_steps: int = 20
    max_tokens: int = 100_000
    timeout_seconds: float = 300.0

    def __post_init__(self) -> None:
        if (
            min(
                self.max_depth,
                self.max_active_children,
                self.max_steps,
                self.max_tokens,
            )
            < 1
        ):
            raise ValueError(
                "Subagent depth, active child, step, and token limits must be positive"
            )
        if self.timeout_seconds <= 0:
            raise ValueError("Subagent timeout must be positive")


@dataclass(frozen=True, slots=True)
class SubagentParentContext:
    run_id: str
    depth: int = 0
    task_id: str | None = None
    root_run_id: str | None = None

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise ValueError("Subagent parent run ID must not be blank")
        if self.depth < 0:
            raise ValueError("Subagent depth must not be negative")
        if self.task_id is not None and not self.task_id.strip():
            raise ValueError("Subagent parent task ID must be non-empty or null")
        if self.root_run_id is not None and not self.root_run_id.strip():
            raise ValueError("Subagent root run ID must be non-empty or null")

    @property
    def owner_run_id(self) -> str:
        return self.root_run_id or self.run_id


@dataclass(frozen=True, slots=True)
class SubagentSpec:
    prompt: str
    description: str
    allowed_tools: tuple[str, ...] | None = None
    attachments: tuple[AttachmentPayload, ...] = ()

    def __post_init__(self) -> None:
        if not self.prompt.strip() or not self.description.strip():
            raise ValueError("Subagent prompt and description must not be blank")
        if self.allowed_tools is not None:
            if not self.allowed_tools or any(
                not name.strip() for name in self.allowed_tools
            ):
                raise ValueError("Subagent allowed tools must be non-empty names")
            if len(self.allowed_tools) != len(set(self.allowed_tools)):
                raise ValueError("Subagent allowed tools must be unique")


@dataclass(frozen=True, slots=True)
class StartedSubagent:
    task_id: str
    run_id: str


@dataclass(frozen=True, slots=True)
class CompletedSubagent:
    task: TaskSnapshot
    run_id: str
    outcome: AgentTurnOutcome | None


@dataclass(frozen=True, slots=True)
class BackgroundSubagent:
    task: TaskSnapshot
    run_id: str
    description: str

    def __post_init__(self) -> None:
        if not self.run_id.strip() or not self.description.strip():
            raise ValueError("Background Subagent identity must not be blank")


__all__ = [
    "BackgroundSubagent",
    "CompletedSubagent",
    "StartedSubagent",
    "SubagentLimits",
    "SubagentParentContext",
    "SubagentSpec",
]
