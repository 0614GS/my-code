"""内置工具的确定性权限优先级。"""

from collections.abc import Iterable

from nano_code.messages import JsonObject
from nano_code.permissions.models import (
    PermissionBehavior,
    PermissionDecision,
    PermissionMode,
    PermissionRule,
    ToolPermissionBehavior,
    ToolPermissionContext,
)
from nano_code.tools.base import Tool, ToolContext, ToolRisk


class PermissionPolicy:
    """组合全局规则、工具判断与模式，不处理 UI。"""

    def __init__(
        self,
        mode: PermissionMode = PermissionMode.DEFAULT,
        rules: Iterable[PermissionRule] = (),
    ) -> None:
        self.mode = mode
        self.rules = tuple(rules)

    async def decide(
        self, tool: Tool, tool_input: JsonObject, context: ToolContext
    ) -> PermissionDecision:
        """组合全局策略与基于具体输入的工具局部判断。"""

        # 所有宽松模式之前都要检查显式 deny 和 ask 规则。
        # 尤其是 bypassPermissions 不能静默抹除用户策略。
        deny_rule = self._whole_tool_rule(tool, PermissionBehavior.DENY)
        if deny_rule is not None:
            return PermissionDecision(
                behavior=PermissionBehavior.DENY,
                message=f"{tool.definition.name} is denied by an explicit rule.",
                reason=f"rule:{deny_rule.source}",
            )

        ask_rule = self._whole_tool_rule(tool, PermissionBehavior.ASK)
        if ask_rule is not None:
            return PermissionDecision(
                behavior=PermissionBehavior.ASK,
                message=f"{tool.definition.name} requires confirmation by rule.",
                reason=f"rule:{ask_rule.source}",
            )

        tool_result = await tool.check_permissions(
            tool_input,
            ToolPermissionContext(
                mode=self.mode,
                rules=self.rules,
                tool_context=context,
            ),
        )

        # 工具拥有输入语义，因此它的拒绝具有权威性。显式内容询问和受保护路径检查
        # 同样可以声明不受 bypass 影响。
        if tool_result.behavior is ToolPermissionBehavior.DENY:
            return PermissionDecision(
                behavior=PermissionBehavior.DENY,
                message=tool_result.message,
                reason=tool_result.reason,
            )
        if (
            tool_result.behavior is ToolPermissionBehavior.ASK
            and tool_result.bypass_immune
        ):
            return PermissionDecision(
                behavior=PermissionBehavior.ASK,
                message=tool_result.message,
                reason=tool_result.reason,
            )

        # plan 模式根据当前具体调用收缩能力，而不只依赖静态工具类别，
        # 因此只读 Bash 调用仍然可用。
        if self.mode is PermissionMode.PLAN and not tool.is_read_only(
            tool_input, context
        ):
            return PermissionDecision(
                behavior=PermissionBehavior.DENY,
                message=f"{tool.definition.name} is unavailable in plan mode.",
                reason="mode:plan",
            )

        # bypass 只改变默认权限决策，不改变工具级安全边界。
        # 文件系统工具仍会拒绝逃逸工作区和访问受保护路径。
        if self.mode is PermissionMode.BYPASS:
            return PermissionDecision(
                behavior=PermissionBehavior.ALLOW,
                message="Allowed by bypassPermissions mode.",
                reason="mode:bypassPermissions",
                updated_input=_updated_input(tool_result.updated_input, tool_input),
            )

        # 只有处理完更高优先级的反对项后，才考虑显式 allow。
        allow_rule = self._whole_tool_rule(tool, PermissionBehavior.ALLOW)
        if allow_rule is not None:
            return PermissionDecision(
                behavior=PermissionBehavior.ALLOW,
                message=f"{tool.definition.name} is allowed by an explicit rule.",
                reason=f"rule:{allow_rule.source}",
                updated_input=_updated_input(tool_result.updated_input, tool_input),
            )

        if tool_result.behavior is ToolPermissionBehavior.ALLOW:
            return PermissionDecision(
                behavior=PermissionBehavior.ALLOW,
                message=tool_result.message,
                reason=tool_result.reason,
                updated_input=_updated_input(tool_result.updated_input, tool_input),
            )

        if self.mode is PermissionMode.ACCEPT_EDITS and tool.risk is ToolRisk.WRITE:
            return PermissionDecision(
                behavior=PermissionBehavior.ALLOW,
                message="Workspace edits are allowed by acceptEdits mode.",
                reason="mode:acceptEdits",
                updated_input=_updated_input(tool_result.updated_input, tool_input),
            )

        # dontAsk 按拒绝处理：原本需要询问的操作会直接被拒绝。
        if self.mode is PermissionMode.DONT_ASK:
            return PermissionDecision(
                behavior=PermissionBehavior.DENY,
                message=(
                    f"{tool.definition.name} needs confirmation, "
                    "but prompts are disabled."
                ),
                reason="mode:dontAsk",
            )

        # 普通工具局部 ask 与 passthrough 最终进入同一个 UI 边界，
        # 两者差异仍保留在可审计原因中。
        return PermissionDecision(
            behavior=PermissionBehavior.ASK,
            message=tool_result.message,
            reason=tool_result.reason,
            updated_input=tool_result.updated_input,
        )

    def _whole_tool_rule(
        self, tool: Tool, behavior: PermissionBehavior
    ) -> PermissionRule | None:
        for rule in self.rules:
            if (
                rule.tool_name == tool.definition.name
                and rule.behavior is behavior
                and rule.applies_to_entire_tool
            ):
                return rule
        return None


def _updated_input(updated: JsonObject | None, original: JsonObject) -> JsonObject:
    return original if updated is None else updated
