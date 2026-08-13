"""Pure permission types, separated to avoid orchestration import cycles."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from nano_code.messages import JsonObject

if TYPE_CHECKING:
    from nano_code.tools.base import ToolContext


class PermissionMode(StrEnum):
    """User-selectable defaults after explicit rules and safety checks."""

    DEFAULT = "default"
    ACCEPT_EDITS = "acceptEdits"
    PLAN = "plan"
    DONT_ASK = "dontAsk"
    BYPASS = "bypassPermissions"


class PermissionBehavior(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class ToolPermissionBehavior(StrEnum):
    """A tool-local result before the global policy resolves defaults."""

    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"
    PASSTHROUGH = "passthrough"


@dataclass(frozen=True, slots=True)
class PermissionRule:
    """A tool rule; optional content is interpreted by that tool."""

    tool_name: str
    behavior: PermissionBehavior
    rule_content: str | None = None
    source: str = "cli"

    def __post_init__(self) -> None:
        if not self.tool_name.strip():
            raise ValueError("Permission rule tool_name cannot be blank")
        if self.rule_content is not None and not self.rule_content.strip():
            raise ValueError("Permission rule content cannot be blank")
        if not self.source.strip():
            raise ValueError("Permission rule source cannot be blank")

    @property
    def applies_to_entire_tool(self) -> bool:
        return self.rule_content is None


@dataclass(frozen=True, slots=True)
class ToolPermissionContext:
    """Read-only policy facts supplied to a tool-specific permission check."""

    mode: PermissionMode
    rules: tuple[PermissionRule, ...]
    tool_context: ToolContext

    def rules_for(
        self, tool_name: str, behavior: PermissionBehavior
    ) -> tuple[PermissionRule, ...]:
        return tuple(
            rule
            for rule in self.rules
            if rule.tool_name == tool_name
            and rule.behavior is behavior
            and not rule.applies_to_entire_tool
        )


@dataclass(frozen=True, slots=True)
class ToolPermissionResult:
    """A tool's input-aware safety judgment, before global mode handling."""

    behavior: ToolPermissionBehavior
    message: str
    reason: str
    updated_input: JsonObject | None = None
    bypass_immune: bool = False

    @classmethod
    def allow(
        cls, tool_input: JsonObject, *, message: str, reason: str
    ) -> ToolPermissionResult:
        return cls(ToolPermissionBehavior.ALLOW, message, reason, tool_input)

    @classmethod
    def ask(
        cls,
        *,
        message: str,
        reason: str,
        bypass_immune: bool = False,
    ) -> ToolPermissionResult:
        return cls(
            ToolPermissionBehavior.ASK,
            message,
            reason,
            bypass_immune=bypass_immune,
        )

    @classmethod
    def deny(cls, *, message: str, reason: str) -> ToolPermissionResult:
        return cls(ToolPermissionBehavior.DENY, message, reason)

    @classmethod
    def passthrough(cls, *, message: str, reason: str) -> ToolPermissionResult:
        return cls(ToolPermissionBehavior.PASSTHROUGH, message, reason)


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    """An auditable decision before optional user confirmation."""

    behavior: PermissionBehavior
    message: str
    reason: str
    updated_input: JsonObject | None = None


@dataclass(frozen=True, slots=True)
class PermissionConfirmation:
    """An explicit human response, optionally including denial guidance."""

    allowed: bool
    feedback: str | None = None

    def __post_init__(self) -> None:
        if self.allowed and self.feedback is not None:
            raise ValueError("Approval cannot include denial feedback")
        if self.feedback is not None and not self.feedback.strip():
            raise ValueError("Permission feedback cannot be blank")
