"""交互与无头环境中对 ``ask`` 决策的处理。"""

import json
from collections.abc import Callable

from my_code.permissions.models import (
    PermissionConfirmation,
    PermissionPrompt,
)


class TerminalPrompter:
    """通过 stdin 请求一次性权限，同时不阻塞事件循环。"""

    def __init__(self, input_fn: Callable[[str], str] = input) -> None:
        self._input = input_fn

    async def confirm(self, request: PermissionPrompt) -> PermissionConfirmation:
        rendered = json.dumps(request.tool_input, ensure_ascii=False, indent=2)
        can_remember = False
        if request.tool_name == "Bash":
            scope = _suggestion_scope(request)
            choices = f'1. Yes\n2. Yes, and don\'t ask again for "{scope}"\n3. No\n'
        else:
            can_remember = bool(request.decision.suggestions)
            remember = "4. Yes, and don't ask again\n" if can_remember else ""
            choices = "1. Yes\n2. No\n3. No, and tell my-code why\n" + remember
        prompt = (
            f"\nPermission required: {request.tool_name}\n"
            f"{rendered}\n{request.decision.message}\n"
            f"{choices}Choice: "
        )
        try:
            answer = self._input(prompt)
        except (EOFError, KeyboardInterrupt):
            return PermissionConfirmation(False)
        normalized = answer.strip().lower()
        if normalized in {"1", "y", "yes"}:
            return PermissionConfirmation(True)
        if normalized == "2" and request.tool_name == "Bash":
            return PermissionConfirmation(True, updates=request.decision.suggestions)
        if normalized == "4" and request.tool_name != "Bash" and can_remember:
            return PermissionConfirmation(True, updates=request.decision.suggestions)
        if normalized == "3":
            if request.tool_name == "Bash":
                return PermissionConfirmation(False)
            try:
                feedback = self._input("Tell my-code what to do differently: ")
            except (EOFError, KeyboardInterrupt):
                return PermissionConfirmation(False)
            if feedback.strip():
                return PermissionConfirmation(False, feedback.strip())
        return PermissionConfirmation(False)


class HeadlessPrompter:
    """不存在交互式权限 UI 时按拒绝处理。"""

    async def confirm(self, request: PermissionPrompt) -> PermissionConfirmation:
        del request
        return PermissionConfirmation(False)


__all__ = [
    "HeadlessPrompter",
    "TerminalPrompter",
]


def _suggestion_scope(request: PermissionPrompt) -> str:
    updates = request.decision.suggestions
    if not updates or not updates[0].rules:
        return "this exact command"
    return updates[0].rules[0].rule_content or request.tool_name
