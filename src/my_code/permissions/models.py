"""纯权限类型；单独存放以避免编排层循环导入。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from my_code.foundation.json import JsonObject


class PermissionMode(StrEnum):
    """显式规则与安全检查之后，由用户选择的默认行为。"""

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
    """全局策略解析默认行为前的工具局部结果。"""

    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"
    PASSTHROUGH = "passthrough"


class PermissionDecisionKind(StrEnum):
    """权限决定的稳定来源类别。"""

    RULE = "rule"
    MODE = "mode"
    TOOL = "tool"
    SAFETY = "safety"
    USER = "user"


class PermissionPromptCategory(StrEnum):
    TOOL = "tool"
    SANDBOX_ESCALATION = "sandbox_escalation"


class PermissionUpdateDestination(StrEnum):
    """权限更新的生效或持久化目标。"""

    SESSION = "session"
    USER = "userSettings"
    PROJECT = "projectSettings"
    LOCAL = "localSettings"


class PermissionUpdateType(StrEnum):
    ADD_RULES = "addRules"
    REPLACE_RULES = "replaceRules"
    REMOVE_RULES = "removeRules"
    SET_MODE = "setMode"
    ADD_DIRECTORIES = "addDirectories"
    REMOVE_DIRECTORIES = "removeDirectories"


@dataclass(frozen=True, slots=True)
class PermissionRule:
    """工具规则；可选内容由对应工具解释。"""

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
class PermissionDecisionReason:
    """结构化且可稳定序列化的权限决定来源。"""

    kind: PermissionDecisionKind
    detail: str
    rule: PermissionRule | None = None

    def __post_init__(self) -> None:
        if not self.detail.strip():
            raise ValueError("Permission decision reason detail cannot be blank")
        if self.kind is PermissionDecisionKind.RULE and self.rule is None:
            raise ValueError("Rule decision reason requires a rule")

    def __str__(self) -> str:
        if self.rule is not None:
            return f"rule:{self.rule.source}"
        return f"{self.kind.value}:{self.detail}"


@dataclass(frozen=True, slots=True)
class PermissionUpdate:
    """对会话权限 context 或某一 settings scope 的结构化修改。"""

    type: PermissionUpdateType
    destination: PermissionUpdateDestination
    rules: tuple[PermissionRule, ...] = ()
    behavior: PermissionBehavior | None = None
    mode: PermissionMode | None = None
    directories: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        rule_update = self.type in {
            PermissionUpdateType.ADD_RULES,
            PermissionUpdateType.REPLACE_RULES,
            PermissionUpdateType.REMOVE_RULES,
        }
        if rule_update:
            if not self.rules or self.behavior is None:
                raise ValueError("Rule permission update requires rules and behavior")
            if any(rule.behavior is not self.behavior for rule in self.rules):
                raise ValueError("Permission update rules must match update behavior")
        elif self.rules or self.behavior is not None:
            raise ValueError("Non-rule permission update cannot include rules")

        if self.type is PermissionUpdateType.SET_MODE:
            if self.mode is None:
                raise ValueError("setMode permission update requires a mode")
        elif self.mode is not None:
            raise ValueError("Only setMode permission updates can include a mode")

        directory_update = self.type in {
            PermissionUpdateType.ADD_DIRECTORIES,
            PermissionUpdateType.REMOVE_DIRECTORIES,
        }
        if directory_update:
            if not self.directories or any(
                not value.strip() for value in self.directories
            ):
                raise ValueError("Directory permission update requires directories")
        elif self.directories:
            raise ValueError(
                "Only directory permission updates can include directories"
            )

    @classmethod
    def add_rules(
        cls,
        rules: tuple[PermissionRule, ...],
        *,
        destination: PermissionUpdateDestination,
    ) -> PermissionUpdate:
        if not rules:
            raise ValueError("addRules requires at least one rule")
        behavior = rules[0].behavior
        return cls(
            PermissionUpdateType.ADD_RULES,
            destination,
            rules=rules,
            behavior=behavior,
        )


@dataclass(frozen=True, slots=True)
class ToolPermissionContext:
    """提供给工具专属权限检查的只读策略事实。"""

    mode: PermissionMode
    rules: tuple[PermissionRule, ...]
    workspace_root: Path
    additional_working_directories: tuple[str, ...] = ()
    internal_read_root: Path | None = None

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
    """全局模式处理前，工具基于具体输入作出的安全判断。"""

    behavior: ToolPermissionBehavior
    message: str
    decision_reason: PermissionDecisionReason
    updated_input: JsonObject | None = None
    bypass_immune: bool = False
    overrides_ask: bool = False
    suggestions: tuple[PermissionUpdate, ...] = ()

    def __post_init__(self) -> None:
        if self.overrides_ask and self.behavior is not ToolPermissionBehavior.ALLOW:
            raise ValueError("Only an allow result can override ask rules")

    @property
    def reason(self) -> str:
        """兼容日志与旧 adapter 的稳定字符串表示。"""

        return str(self.decision_reason)

    @classmethod
    def allow(
        cls,
        tool_input: JsonObject,
        *,
        message: str,
        reason: PermissionDecisionReason,
        overrides_ask: bool = False,
        suggestions: tuple[PermissionUpdate, ...] = (),
    ) -> ToolPermissionResult:
        return cls(
            ToolPermissionBehavior.ALLOW,
            message,
            reason,
            tool_input,
            overrides_ask=overrides_ask,
            suggestions=suggestions,
        )

    @classmethod
    def ask(
        cls,
        *,
        message: str,
        reason: PermissionDecisionReason,
        bypass_immune: bool = False,
        updated_input: JsonObject | None = None,
        suggestions: tuple[PermissionUpdate, ...] = (),
    ) -> ToolPermissionResult:
        return cls(
            ToolPermissionBehavior.ASK,
            message,
            reason,
            updated_input=updated_input,
            bypass_immune=bypass_immune,
            suggestions=suggestions,
        )

    @classmethod
    def deny(
        cls, *, message: str, reason: PermissionDecisionReason
    ) -> ToolPermissionResult:
        return cls(ToolPermissionBehavior.DENY, message, reason)

    @classmethod
    def passthrough(
        cls,
        *,
        message: str,
        reason: PermissionDecisionReason,
        updated_input: JsonObject | None = None,
        suggestions: tuple[PermissionUpdate, ...] = (),
    ) -> ToolPermissionResult:
        return cls(
            ToolPermissionBehavior.PASSTHROUGH,
            message,
            reason,
            updated_input=updated_input,
            suggestions=suggestions,
        )


@dataclass(frozen=True, slots=True)
class PermissionRequest:
    """Policy input assembled by the controlled tool execution boundary."""

    tool_name: str
    tool_input: JsonObject
    tool_result: ToolPermissionResult

    def __post_init__(self) -> None:
        if not self.tool_name.strip():
            raise ValueError("Permission request tool_name cannot be blank")


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    """可选用户确认前的可审计决策。"""

    behavior: PermissionBehavior
    message: str
    decision_reason: PermissionDecisionReason
    updated_input: JsonObject | None = None
    suggestions: tuple[PermissionUpdate, ...] = ()

    @property
    def reason(self) -> str:
        return str(self.decision_reason)


@dataclass(frozen=True, slots=True)
class PermissionConfirmation:
    """用户的显式响应，可附带拒绝原因或指导。"""

    allowed: bool
    feedback: str | None = None
    updates: tuple[PermissionUpdate, ...] = ()

    def __post_init__(self) -> None:
        if self.allowed and self.feedback is not None:
            raise ValueError("Approval cannot include denial feedback")
        if self.feedback is not None and not self.feedback.strip():
            raise ValueError("Permission feedback cannot be blank")
        if not self.allowed and self.updates:
            raise ValueError("Denial cannot include permission updates")


@dataclass(frozen=True, slots=True)
class PermissionPrompt:
    """A pending user decision without Tool or frontend-specific types."""

    tool_name: str
    tool_input: JsonObject
    decision: PermissionDecision
    display_name: str
    summary: str
    activity: str
    category: PermissionPromptCategory = PermissionPromptCategory.TOOL
    requester: str | None = None
    run_id: str | None = None

    def __post_init__(self) -> None:
        if not self.tool_name.strip():
            raise ValueError("Permission prompt tool_name cannot be blank")
        if not self.display_name.strip():
            raise ValueError("Permission prompt display_name cannot be blank")


class PermissionPrompter(Protocol):
    """Host capability for resolving a pending interactive decision."""

    async def confirm(self, request: PermissionPrompt) -> PermissionConfirmation: ...


__all__ = [
    "PermissionBehavior",
    "PermissionConfirmation",
    "PermissionDecision",
    "PermissionDecisionKind",
    "PermissionDecisionReason",
    "PermissionMode",
    "PermissionPrompt",
    "PermissionPromptCategory",
    "PermissionPrompter",
    "PermissionRequest",
    "PermissionRule",
    "PermissionUpdate",
    "PermissionUpdateDestination",
    "PermissionUpdateType",
    "ToolPermissionBehavior",
    "ToolPermissionContext",
    "ToolPermissionResult",
]
