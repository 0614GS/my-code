"""纯权限类型；单独存放以避免编排层循环导入。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from nano_code.messages import JsonObject

if TYPE_CHECKING:
    from nano_code.tools.base import ToolContext


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
class ToolPermissionContext:
    """提供给工具专属权限检查的只读策略事实。"""

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
    """全局模式处理前，工具基于具体输入作出的安全判断。"""

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
    """可选用户确认前的可审计决策。"""

    behavior: PermissionBehavior
    message: str
    reason: str
    updated_input: JsonObject | None = None


@dataclass(frozen=True, slots=True)
class PermissionConfirmation:
    """用户的显式响应，可附带拒绝原因或指导。"""

    allowed: bool
    feedback: str | None = None

    def __post_init__(self) -> None:
        if self.allowed and self.feedback is not None:
            raise ValueError("Approval cannot include denial feedback")
        if self.feedback is not None and not self.feedback.strip():
            raise ValueError("Permission feedback cannot be blank")
