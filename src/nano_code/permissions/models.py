"""Pure permission types, separated to avoid orchestration import cycles."""

from dataclasses import dataclass
from enum import StrEnum


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


@dataclass(frozen=True, slots=True)
class PermissionRule:
    """An exact tool-name rule for the first MVP."""

    tool_name: str
    behavior: PermissionBehavior
    source: str = "cli"


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    """An auditable decision before optional user confirmation."""

    behavior: PermissionBehavior
    message: str
    reason: str
