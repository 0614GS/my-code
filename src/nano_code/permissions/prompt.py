"""交互与无头环境中对 ``ask`` 决策的处理。"""

import json
from collections.abc import Callable
from typing import Protocol

from nano_code.model import JsonObject
from nano_code.permissions.models import (
    PermissionBehavior,
    PermissionConfirmation,
    PermissionDecision,
    PermissionUpdate,
    PermissionUpdateDestination,
)
from nano_code.permissions.rules import validate_bash_rule_content
from nano_code.permissions.updates import permission_rule_for_destination
from nano_code.tools.base import Tool


class PermissionPrompter(Protocol):
    async def confirm(
        self, tool: Tool, tool_input: JsonObject, decision: PermissionDecision
    ) -> PermissionConfirmation:
        """仅在获得用户显式输入后返回结构化响应。"""
        ...


class TerminalPrompter:
    """通过 stdin 请求一次性权限，同时不阻塞事件循环。"""

    def __init__(self, input_fn: Callable[[str], str] = input) -> None:
        self._input = input_fn

    async def confirm(
        self, tool: Tool, tool_input: JsonObject, decision: PermissionDecision
    ) -> PermissionConfirmation:
        rendered = json.dumps(tool_input, ensure_ascii=False, indent=2)
        can_remember = tool.definition.name == "Bash" or bool(decision.suggestions)
        remember = "4. Yes, and don't ask again\n" if can_remember else ""
        prompt = (
            f"\nPermission required: {tool.definition.name}\n"
            f"{rendered}\n{decision.message}\n"
            "1. Yes\n2. No\n3. No, and tell nano-code why\n"
            f"{remember}Choice: "
        )
        try:
            answer = self._input(prompt)
        except (EOFError, KeyboardInterrupt):
            return PermissionConfirmation(False)
        normalized = answer.strip().lower()
        if normalized in {"1", "y", "yes"}:
            return PermissionConfirmation(True)
        if normalized == "4" and can_remember:
            try:
                updates = self._remember_updates(tool, decision)
            except (EOFError, KeyboardInterrupt):
                return PermissionConfirmation(False)
            return PermissionConfirmation(True, updates=updates)
        if normalized == "3":
            try:
                feedback = self._input("Tell nano-code what to do differently: ")
            except (EOFError, KeyboardInterrupt):
                return PermissionConfirmation(False)
            if feedback.strip():
                return PermissionConfirmation(False, feedback.strip())
        return PermissionConfirmation(False)

    def _remember_updates(
        self, tool: Tool, decision: PermissionDecision
    ) -> tuple[PermissionUpdate, ...]:
        """构造由当前确认框明确提供的长期授权更新。"""

        if tool.definition.name == "Bash":
            while True:
                raw = self._input(
                    "Command prefix to allow (e.g., git diff:*): "
                ).strip()
                try:
                    content = validate_bash_rule_content(raw)
                except ValueError as error:
                    print(f"Invalid command prefix: {error}")
                    continue
                rule = permission_rule_for_destination(
                    "Bash",
                    PermissionBehavior.ALLOW,
                    PermissionUpdateDestination.LOCAL,
                    content,
                )
                return (
                    PermissionUpdate.add_rules(
                        (rule,), destination=PermissionUpdateDestination.LOCAL
                    ),
                )
        return decision.suggestions


class HeadlessPrompter:
    """不存在交互式权限 UI 时按拒绝处理。"""

    async def confirm(
        self, tool: Tool, tool_input: JsonObject, decision: PermissionDecision
    ) -> PermissionConfirmation:
        del tool, tool_input, decision
        return PermissionConfirmation(False)
