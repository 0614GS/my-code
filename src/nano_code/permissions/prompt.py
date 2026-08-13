"""Interactive and headless resolution of ``ask`` decisions."""

import json
from collections.abc import Callable
from typing import Protocol

from nano_code.messages import JsonObject
from nano_code.permissions.models import PermissionConfirmation, PermissionDecision
from nano_code.tools.base import Tool


class PermissionPrompter(Protocol):
    async def confirm(
        self, tool: Tool, tool_input: JsonObject, decision: PermissionDecision
    ) -> PermissionConfirmation:
        """Return a structured response only after explicit human input."""
        ...


class TerminalPrompter:
    """Ask for one-time permission on stdin without blocking the event loop."""

    def __init__(self, input_fn: Callable[[str], str] = input) -> None:
        self._input = input_fn

    async def confirm(
        self, tool: Tool, tool_input: JsonObject, decision: PermissionDecision
    ) -> PermissionConfirmation:
        rendered = json.dumps(tool_input, ensure_ascii=False, indent=2)
        prompt = (
            f"\nPermission required: {tool.definition.name}\n"
            f"{rendered}\n{decision.message}\n"
            "1. Yes\n2. No\n3. No, and tell nano-code why\nChoice: "
        )
        try:
            answer = self._input(prompt)
        except (EOFError, KeyboardInterrupt):
            return PermissionConfirmation(False)
        normalized = answer.strip().lower()
        if normalized in {"1", "y", "yes"}:
            return PermissionConfirmation(True)
        if normalized == "3":
            try:
                feedback = self._input("Tell nano-code what to do differently: ")
            except (EOFError, KeyboardInterrupt):
                return PermissionConfirmation(False)
            if feedback.strip():
                return PermissionConfirmation(False, feedback.strip())
        return PermissionConfirmation(False)


class HeadlessPrompter:
    """Fail closed when no interactive permission UI exists."""

    async def confirm(
        self, tool: Tool, tool_input: JsonObject, decision: PermissionDecision
    ) -> PermissionConfirmation:
        del tool, tool_input, decision
        return PermissionConfirmation(False)
