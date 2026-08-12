"""Interactive and headless resolution of ``ask`` decisions."""

import json
from collections.abc import Callable
from typing import Protocol

from nano_code.messages import JsonObject
from nano_code.permissions.models import PermissionDecision
from nano_code.tools.base import Tool


class PermissionPrompter(Protocol):
    async def confirm(
        self, tool: Tool, tool_input: JsonObject, decision: PermissionDecision
    ) -> bool:
        """Return true only for an explicit one-time approval."""


class TerminalPrompter:
    """Ask for one-time permission on stdin without blocking the event loop."""

    def __init__(self, input_fn: Callable[[str], str] = input) -> None:
        self._input = input_fn

    async def confirm(
        self, tool: Tool, tool_input: JsonObject, decision: PermissionDecision
    ) -> bool:
        rendered = json.dumps(tool_input, ensure_ascii=False, indent=2)
        prompt = (
            f"\nPermission required: {tool.definition.name}\n"
            f"{rendered}\n{decision.message} [y/N] "
        )
        try:
            answer = self._input(prompt)
        except (EOFError, KeyboardInterrupt):
            return False
        return answer.strip().lower() in {"y", "yes"}


class HeadlessPrompter:
    """Fail closed when no interactive permission UI exists."""

    async def confirm(
        self, tool: Tool, tool_input: JsonObject, decision: PermissionDecision
    ) -> bool:
        del tool, tool_input, decision
        return False
