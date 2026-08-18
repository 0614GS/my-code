"""Metadata and lifecycle ports for every controlled tool invocation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from nano_code.conversation import JsonObject, ToolCall, ToolResult

if TYPE_CHECKING:
    from nano_code.permissions.models import PermissionDecision
    from nano_code.tools.base import Tool, ToolContext


class ToolInvocationOrigin(StrEnum):
    """The application capability that initiated a tool call."""

    MODEL = "model"
    USER_FILE_MENTION = "user_file_mention"


class AuthorizationEvidence(StrEnum):
    """Trusted evidence that can satisfy an ordinary permission prompt."""

    NONE = "none"
    EXPLICIT_USER_INPUT = "explicit_user_input"


class ToolResultDelivery(StrEnum):
    """How a successful model-visible result leaves the execution boundary."""

    EXTERNALIZED = "externalized"
    INLINE = "inline"


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    """Security-relevant metadata for one invocation."""

    origin: ToolInvocationOrigin = ToolInvocationOrigin.MODEL
    authorization: AuthorizationEvidence = AuthorizationEvidence.NONE
    result_delivery: ToolResultDelivery = ToolResultDelivery.EXTERNALIZED

    @classmethod
    def explicit_file_mention(cls) -> ToolInvocation:
        return cls(
            origin=ToolInvocationOrigin.USER_FILE_MENTION,
            authorization=AuthorizationEvidence.EXPLICIT_USER_INPUT,
            result_delivery=ToolResultDelivery.INLINE,
        )


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
    "AuthorizationEvidence",
    "ToolInvocation",
    "ToolInvocationAudit",
    "ToolInvocationHook",
    "ToolInvocationOrigin",
    "ToolResultDelivery",
]
