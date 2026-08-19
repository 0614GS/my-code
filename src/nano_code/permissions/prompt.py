"""交互与无头环境中对 ``ask`` 决策的处理。"""

import json
from collections.abc import Callable

from nano_code.permissions.models import (
    PermissionBehavior,
    PermissionConfirmation,
    PermissionPrompt,
    PermissionUpdate,
    PermissionUpdateDestination,
)
from nano_code.permissions.rules import validate_bash_rule_content
from nano_code.permissions.updates import permission_rule_for_destination


class TerminalPrompter:
    """通过 stdin 请求一次性权限，同时不阻塞事件循环。"""

    def __init__(self, input_fn: Callable[[str], str] = input) -> None:
        self._input = input_fn

    async def confirm(self, request: PermissionPrompt) -> PermissionConfirmation:
        rendered = json.dumps(request.tool_input, ensure_ascii=False, indent=2)
        can_remember = request.tool_name == "Bash" or bool(request.decision.suggestions)
        remember = "4. Yes, and don't ask again\n" if can_remember else ""
        prompt = (
            f"\nPermission required: {request.tool_name}\n"
            f"{rendered}\n{request.decision.message}\n"
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
                updates = self._remember_updates(request)
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
        self, request: PermissionPrompt
    ) -> tuple[PermissionUpdate, ...]:
        """构造由当前确认框明确提供的长期授权更新。"""

        if request.tool_name == "Bash":
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
        return request.decision.suggestions


class HeadlessPrompter:
    """不存在交互式权限 UI 时按拒绝处理。"""

    async def confirm(self, request: PermissionPrompt) -> PermissionConfirmation:
        del request
        return PermissionConfirmation(False)


__all__ = [
    "HeadlessPrompter",
    "TerminalPrompter",
]
