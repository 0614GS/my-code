"""Frontend-neutral permission request and mode values."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from my_code.foundation.json import JsonObject
from my_code.permissions.models import (
    PermissionConfirmation,
    PermissionPromptCategory,
    PermissionUpdate,
)
from my_code.tools.presentation import ToolUsePresentation


@dataclass(frozen=True, slots=True)
class PermissionRequest:
    tool_name: str
    tool_input: JsonObject
    message: str
    presentation: ToolUsePresentation
    suggestions: tuple[PermissionUpdate, ...] = ()
    category: PermissionPromptCategory = PermissionPromptCategory.TOOL
    requester: str | None = None
    run_id: str | None = None


@dataclass(frozen=True, slots=True)
class PermissionModeView:
    value: str
    display_name: str
    current: bool
    dangerous: bool
    sandbox_active: bool
    requires_confirmation: bool


@dataclass(frozen=True, slots=True)
class PermissionModeSwitch:
    mode: PermissionModeView
    changed: bool
    requires_confirmation: bool


type PermissionHandler = Callable[
    [PermissionRequest], Awaitable[PermissionConfirmation]
]


__all__ = [
    "PermissionHandler",
    "PermissionModeSwitch",
    "PermissionModeView",
    "PermissionRequest",
]
