"""Core tool protocol, intentionally independent from providers and UI."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from nano_code.messages import JsonObject

if TYPE_CHECKING:
    from nano_code.permissions.models import (
        ToolPermissionContext,
        ToolPermissionResult,
    )


class ToolRisk(StrEnum):
    """The side-effect class consumed by the permission engine."""

    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """The stable tool identity and schema exposed to a model."""

    name: str
    description: str
    input_schema: JsonObject


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Runtime dependencies available to built-in tools."""

    cwd: Path
    command_timeout_seconds: float = 120.0
    max_command_output_bytes: int = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ToolOutput:
    """Provider-neutral output from a tool implementation."""

    content: str
    is_error: bool = False


class ToolInputError(ValueError):
    """Raised when input fails schema-adjacent or semantic validation."""


class ToolExecutionError(RuntimeError):
    """Raised when a valid tool request cannot be completed."""


class Tool(ABC):
    """A typed unit of validation, permission metadata, and execution."""

    @property
    @abstractmethod
    def definition(self) -> ToolDefinition:
        """Return the model-visible definition."""

    @property
    @abstractmethod
    def risk(self) -> ToolRisk:
        """Return the default side-effect class."""

    @property
    def concurrency_safe(self) -> bool:
        """Whether calls may be parallelized once the scheduler supports it."""

        return False

    def is_read_only(self, tool_input: JsonObject, context: ToolContext) -> bool:
        """Describe this concrete call's side-effect semantics.

        Dynamic tools such as Bash override this method. This metadata supports
        permission, scheduling, and UI code; it does not grant access itself.
        """

        del tool_input, context
        return self.risk is ToolRisk.READ

    async def check_permissions(
        self, tool_input: JsonObject, context: ToolPermissionContext
    ) -> ToolPermissionResult:
        """Return the tool-local judgment consumed by the global policy."""

        # A local import prevents a runtime cycle: the permission policy imports
        # Tool, while the provider/UI-independent protocol only needs the type.
        from nano_code.permissions.models import ToolPermissionResult

        if self.is_read_only(tool_input, context.tool_context):
            return ToolPermissionResult.allow(
                tool_input,
                message="This call is read-only.",
                reason="tool:read-only",
            )
        return ToolPermissionResult.passthrough(
            message=f"{self.definition.name} requires approval for this call.",
            reason="tool:passthrough",
        )

    @abstractmethod
    def validate_input(self, tool_input: JsonObject) -> None:
        """Raise ``ToolInputError`` before permission evaluation on bad input."""

    @abstractmethod
    async def execute(self, tool_input: JsonObject, context: ToolContext) -> ToolOutput:
        """Execute a validated, permitted call."""
