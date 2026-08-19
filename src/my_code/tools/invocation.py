"""Metadata and lifecycle ports for every controlled tool invocation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from my_code.conversation.models import ToolCall, ToolResult
from my_code.model.primitives import JsonObject

if TYPE_CHECKING:
    from my_code.permissions.models import PermissionDecision
    from my_code.tools.base import Tool, ToolContext


class ToolInvocationOrigin(StrEnum):
    """The application capability that initiated a tool call."""

    MODEL = "model"


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    """Security-relevant metadata for one invocation."""

    origin: ToolInvocationOrigin = ToolInvocationOrigin.MODEL


class ToolInvocationHook(Protocol):
    """Lifecycle hook run inside the controlled invocation boundary."""

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
