"""Metadata and lifecycle ports for every controlled tool invocation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from my_code.conversation.models import ToolCall, ToolResult
from my_code.foundation.json import JsonObject

if TYPE_CHECKING:
    from my_code.permissions.models import PermissionDecision
    from my_code.tools.base import Tool, ToolContext


class ToolInvocationOrigin(StrEnum):
    """The application capability that initiated a tool call."""

    MODEL = "model"
    SEARCHED_DISPATCH = "searched_dispatch"


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    """Security-relevant metadata for one invocation."""

    origin: ToolInvocationOrigin = ToolInvocationOrigin.MODEL
    dispatcher_name: str | None = None
    target_name: str | None = None


class ToolInvocationHook(Protocol):
    """Observational lifecycle hook inside the controlled invocation boundary.

    Calls and approved inputs are isolated snapshots. Mutating them never changes
    the persisted ToolCall or the input subsequently passed to the Tool.
    """

    async def before_execute(
        self,
        invocation: ToolInvocation,
        call: ToolCall,
        tool: Tool,
        approved_input: JsonObject,
        context: ToolContext,
    ) -> None: ...

    async def after_execute(
        self,
        invocation: ToolInvocation,
        call: ToolCall,
        result: ToolResult,
    ) -> None: ...


class ToolInvocationAudit(Protocol):
    """Mandatory audit sink; failure prevents a not-yet-started invocation."""

    async def record_permission(
        self,
        invocation: ToolInvocation,
        call: ToolCall,
        decision: PermissionDecision,
    ) -> None: ...


__all__ = [
    "ToolInvocation",
    "ToolInvocationAudit",
    "ToolInvocationHook",
    "ToolInvocationOrigin",
]
