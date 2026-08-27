"""Foreground/background-neutral Subagent product values."""

from dataclasses import dataclass
from enum import StrEnum

from my_code.agent.models import AgentTurnOutcome
from my_code.conversation.attachments import AttachmentPayload
from my_code.prompts.registry import PromptRegistry
from my_code.tasks.models import TaskSnapshot


class SubagentType(StrEnum):
    EXPLORE = "explore"
    GENERAL = "general"


@dataclass(frozen=True, slots=True)
class SubagentDefinition:
    agent_type: SubagentType
    description: str
    system_prompt: PromptRegistry
    tool_names: tuple[str, ...] | None
    read_only: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.agent_type, SubagentType):
            raise ValueError("Subagent definition type must be explore or general")
        if not self.description.strip():
            raise ValueError("Subagent definition description must not be blank")
        if self.tool_names is not None and (
            not self.tool_names
            or any(not name.strip() for name in self.tool_names)
            or len(self.tool_names) != len(set(self.tool_names))
        ):
            raise ValueError("Subagent definition tools must be unique non-empty names")


@dataclass(frozen=True, slots=True)
class SubagentLimits:
    max_depth: int = 3
    max_active_children: int = 4
    max_steps: int | None = None
    max_tokens: int | None = None
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        if min(self.max_depth, self.max_active_children) < 1:
            raise ValueError("Subagent depth and active child limits must be positive")
        if self.max_steps is not None and self.max_steps < 1:
            raise ValueError("Subagent step limit must be positive or null")
        if self.max_tokens is not None and self.max_tokens < 1:
            raise ValueError("Subagent token limit must be positive or null")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("Subagent timeout must be positive or null")


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
    agent_type: SubagentType
    prompt: str
    description: str
    attachments: tuple[AttachmentPayload, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.agent_type, SubagentType):
            raise ValueError("Subagent type must be explore or general")
        if not self.prompt.strip() or not self.description.strip():
            raise ValueError("Subagent prompt and description must not be blank")


@dataclass(frozen=True, slots=True)
class StartedSubagent:
    task_id: str
    run_id: str
    agent_type: SubagentType

    def __post_init__(self) -> None:
        if not self.task_id.strip() or not self.run_id.strip():
            raise ValueError("Started Subagent identity must not be blank")
        if not isinstance(self.agent_type, SubagentType):
            raise ValueError("Started Subagent type must be explore or general")


@dataclass(frozen=True, slots=True)
class CompletedSubagent:
    task: TaskSnapshot
    run_id: str
    outcome: AgentTurnOutcome | None
    agent_type: SubagentType

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise ValueError("Completed Subagent run ID must not be blank")
        if not isinstance(self.agent_type, SubagentType):
            raise ValueError("Completed Subagent type must be explore or general")


@dataclass(frozen=True, slots=True)
class BackgroundSubagent:
    task: TaskSnapshot
    run_id: str
    description: str
    agent_type: SubagentType

    def __post_init__(self) -> None:
        if not self.run_id.strip() or not self.description.strip():
            raise ValueError("Background Subagent identity must not be blank")
        if not isinstance(self.agent_type, SubagentType):
            raise ValueError("Background Subagent type must be explore or general")


__all__ = [
    "BackgroundSubagent",
    "CompletedSubagent",
    "StartedSubagent",
    "SubagentDefinition",
    "SubagentLimits",
    "SubagentParentContext",
    "SubagentSpec",
    "SubagentType",
]
