"""Provider-neutral structured payloads carried by ``AttachmentMessage``."""

from dataclasses import dataclass
from typing import Literal

from my_code.foundation.json import JsonObject, to_json_object


@dataclass(frozen=True, slots=True)
class FileMentionAttachment:
    path: str
    body: str
    is_directory: bool = False
    kind: Literal["file_mention"] = "file_mention"

    def __post_init__(self) -> None:
        if not self.path.strip() or not self.body:
            raise ValueError("File attachment path and body must not be empty")


@dataclass(frozen=True, slots=True)
class TodoReminderAttachment:
    content: str
    kind: Literal["todo_reminder"] = "todo_reminder"

    def __post_init__(self) -> None:
        if not self.content.strip():
            raise ValueError("Todo reminder must not be empty")


@dataclass(frozen=True, slots=True)
class BackgroundTaskCompletionAttachment:
    owner_run_id: str
    task_id: str
    result: JsonObject
    kind: Literal["background_task_completion"] = "background_task_completion"

    def __post_init__(self) -> None:
        if not self.owner_run_id.strip() or not self.task_id.strip():
            raise ValueError("Background task identity must not be empty")
        object.__setattr__(self, "result", to_json_object(self.result))


@dataclass(frozen=True, slots=True)
class SkillListingEntry:
    name: str
    description: str
    source: str

    def __post_init__(self) -> None:
        if not (self.name.strip() and self.description.strip() and self.source.strip()):
            raise ValueError("Skill listing fields must not be empty")


@dataclass(frozen=True, slots=True)
class SkillListingAttachment:
    catalog_version: int
    skills: tuple[SkillListingEntry, ...]
    kind: Literal["skill_listing"] = "skill_listing"

    def __post_init__(self) -> None:
        if self.catalog_version < 0:
            raise ValueError("Skill catalog version must not be negative")
        if not self.skills:
            raise ValueError("Skill listing must not be empty")


@dataclass(frozen=True, slots=True)
class SkillActivationAttachment:
    name: str
    instructions: str
    source: str
    locator: str
    compatibility: str | None = None
    allowed_tools: tuple[str, ...] = ()
    kind: Literal["skill_activation"] = "skill_activation"

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (self.name, self.instructions, self.source, self.locator)
        ):
            raise ValueError("Activated Skill fields must not be empty")
        if self.compatibility is not None and not self.compatibility.strip():
            raise ValueError("Skill compatibility must be non-empty or null")


@dataclass(frozen=True, slots=True)
class InvokedSkillsAttachment:
    skills: tuple[SkillActivationAttachment, ...]
    kind: Literal["invoked_skills"] = "invoked_skills"

    def __post_init__(self) -> None:
        if not self.skills:
            raise ValueError("Invoked Skills attachment must not be empty")
        names = [skill.name for skill in self.skills]
        if len(names) != len(set(names)):
            raise ValueError("Invoked Skills attachment contains duplicate names")


@dataclass(frozen=True, slots=True)
class ToolDiscoveryDefinition:
    name: str
    description: str
    input_schema: JsonObject
    fingerprint: str

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.fingerprint.strip():
            raise ValueError("Tool discovery identity must not be empty")
        object.__setattr__(self, "input_schema", to_json_object(self.input_schema))


@dataclass(frozen=True, slots=True)
class ToolDiscoveryAttachment:
    definitions: tuple[ToolDiscoveryDefinition, ...]
    mode: Literal["dispatcher", "native"]
    kind: Literal["tool_discovery"] = "tool_discovery"

    def __post_init__(self) -> None:
        if not self.definitions:
            raise ValueError("Tool discovery must contain at least one definition")
        names = [item.name for item in self.definitions]
        if len(names) != len(set(names)):
            raise ValueError("Tool discovery contains duplicate names")


@dataclass(frozen=True, slots=True)
class ToolDiscoveryInvalidationAttachment:
    names: tuple[str, ...]
    kind: Literal["tool_discovery_invalidation"] = "tool_discovery_invalidation"

    def __post_init__(self) -> None:
        if not self.names or any(not name.strip() for name in self.names):
            raise ValueError("Tool discovery invalidation requires tool names")
        if len(self.names) != len(set(self.names)):
            raise ValueError("Tool discovery invalidation contains duplicate names")


@dataclass(frozen=True, slots=True)
class ToolSearchListingAttachment:
    names: tuple[str, ...]
    kind: Literal["tool_search_listing"] = "tool_search_listing"

    def __post_init__(self) -> None:
        if any(not name.strip() for name in self.names):
            raise ValueError("Tool search listing names must not be empty")
        if tuple(sorted(set(self.names))) != self.names:
            raise ValueError("Tool search listing names must be sorted and unique")


@dataclass(frozen=True, slots=True)
class CollaborationModeAttachment:
    mode: Literal["default", "plan"]
    kind: Literal["collaboration_mode"] = "collaboration_mode"


@dataclass(frozen=True, slots=True)
class PlanHandoffAttachment:
    plan: str
    kind: Literal["plan_handoff"] = "plan_handoff"

    def __post_init__(self) -> None:
        if not self.plan.strip():
            raise ValueError("Plan handoff must not be empty")


type AttachmentPayload = (
    FileMentionAttachment
    | TodoReminderAttachment
    | BackgroundTaskCompletionAttachment
    | SkillListingAttachment
    | SkillActivationAttachment
    | InvokedSkillsAttachment
    | ToolDiscoveryAttachment
    | ToolDiscoveryInvalidationAttachment
    | ToolSearchListingAttachment
    | CollaborationModeAttachment
    | PlanHandoffAttachment
)


def is_durable_attachment(payload: AttachmentPayload) -> bool:
    """Central persistence policy, deliberately independent of producers."""

    return isinstance(
        payload,
        (
            FileMentionAttachment,
            BackgroundTaskCompletionAttachment,
            SkillActivationAttachment,
            InvokedSkillsAttachment,
            ToolDiscoveryAttachment,
            ToolDiscoveryInvalidationAttachment,
            CollaborationModeAttachment,
            PlanHandoffAttachment,
        ),
    )


__all__ = [
    "AttachmentPayload",
    "BackgroundTaskCompletionAttachment",
    "CollaborationModeAttachment",
    "FileMentionAttachment",
    "InvokedSkillsAttachment",
    "PlanHandoffAttachment",
    "SkillActivationAttachment",
    "SkillListingAttachment",
    "SkillListingEntry",
    "TodoReminderAttachment",
    "ToolDiscoveryAttachment",
    "ToolDiscoveryDefinition",
    "ToolDiscoveryInvalidationAttachment",
    "ToolSearchListingAttachment",
    "is_durable_attachment",
]
