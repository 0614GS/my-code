"""内置工具的确定性权限优先级。"""

from collections.abc import Iterable

from nano_code.messages import JsonObject
from nano_code.permissions.models import (
    PermissionBehavior,
    PermissionDecision,
    PermissionDecisionKind,
    PermissionDecisionReason,
    PermissionMode,
    PermissionRule,
    PermissionUpdate,
    PermissionUpdateType,
    ToolPermissionBehavior,
    ToolPermissionContext,
)
from nano_code.tools.base import Tool, ToolContext


class PermissionPolicy:
    """组合全局规则、工具判断与模式，不处理 UI。"""

    def __init__(
        self,
        mode: PermissionMode = PermissionMode.DEFAULT,
        rules: Iterable[PermissionRule] = (),
    ) -> None:
        self.mode = mode
        self.rules = tuple(rules)

    def add_rules(self, rules: Iterable[PermissionRule]) -> None:
        """把规则注入当前权限 context。持久化由 update applier 负责。"""

        existing = {
            (rule.tool_name, rule.behavior.value, rule.rule_content)
            for rule in self.rules
        }
        additions: list[PermissionRule] = []
        for rule in rules:
            key = (rule.tool_name, rule.behavior.value, rule.rule_content)
            if key in existing:
                continue
            existing.add(key)
            additions.append(rule)
        self.rules = self.rules + tuple(additions)

    def replace_rules(
        self,
        behavior: PermissionBehavior,
        source: str,
        rules: Iterable[PermissionRule],
    ) -> None:
        retained = tuple(
            rule
            for rule in self.rules
            if rule.behavior is not behavior or rule.source != source
        )
        self.rules = retained
        self.add_rules(rules)

    def remove_rules(self, rules: Iterable[PermissionRule]) -> None:
        removed = {
            (rule.tool_name, rule.behavior, rule.rule_content, rule.source)
            for rule in rules
        }
        self.rules = tuple(
            rule
            for rule in self.rules
            if (rule.tool_name, rule.behavior, rule.rule_content, rule.source)
            not in removed
        )

    def apply_update(self, update: PermissionUpdate) -> None:
        """应用已经过持久化层验证的权限更新。"""

        if update.type is PermissionUpdateType.ADD_RULES:
            self.add_rules(update.rules)
        elif update.type is PermissionUpdateType.REPLACE_RULES:
            assert update.behavior is not None
            self.replace_rules(update.behavior, update.destination.value, update.rules)
        elif update.type is PermissionUpdateType.REMOVE_RULES:
            self.remove_rules(update.rules)
        elif update.type is PermissionUpdateType.SET_MODE:
            assert update.mode is not None
            self.mode = update.mode
        else:
            raise ValueError("Additional working directories are not supported yet")

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
                decision_reason=_rule_reason(deny_rule),
            )

        ask_rule = self._whole_tool_rule(tool, PermissionBehavior.ASK)
        if ask_rule is not None:
            return PermissionDecision(
                behavior=PermissionBehavior.ASK,
                message=f"{tool.definition.name} requires confirmation by rule.",
                decision_reason=_rule_reason(ask_rule),
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
                decision_reason=tool_result.decision_reason,
                updated_input=tool_result.updated_input,
                suggestions=tool_result.suggestions,
            )
        if (
            tool_result.behavior is ToolPermissionBehavior.ASK
            and tool_result.bypass_immune
        ):
            return PermissionDecision(
                behavior=PermissionBehavior.ASK,
                message=tool_result.message,
                decision_reason=tool_result.decision_reason,
                updated_input=tool_result.updated_input,
                suggestions=tool_result.suggestions,
            )

        # bypass 只改变默认权限决策，不改变工具级安全边界。
        # 文件系统工具仍会拒绝逃逸工作区和访问受保护路径。
        if self.mode is PermissionMode.BYPASS:
            return PermissionDecision(
                behavior=PermissionBehavior.ALLOW,
                message="Allowed by bypassPermissions mode.",
                decision_reason=PermissionDecisionReason(
                    PermissionDecisionKind.MODE, PermissionMode.BYPASS.value
                ),
                updated_input=_updated_input(tool_result.updated_input, tool_input),
            )

        # 只有处理完更高优先级的反对项后，才考虑显式 allow。
        allow_rule = self._whole_tool_rule(tool, PermissionBehavior.ALLOW)
        if allow_rule is not None:
            return PermissionDecision(
                behavior=PermissionBehavior.ALLOW,
                message=f"{tool.definition.name} is allowed by an explicit rule.",
                decision_reason=_rule_reason(allow_rule),
                updated_input=_updated_input(tool_result.updated_input, tool_input),
            )

        if tool_result.behavior is ToolPermissionBehavior.ALLOW:
            return PermissionDecision(
                behavior=PermissionBehavior.ALLOW,
                message=tool_result.message,
                decision_reason=tool_result.decision_reason,
                updated_input=_updated_input(tool_result.updated_input, tool_input),
                suggestions=tool_result.suggestions,
            )

        # dontAsk 按拒绝处理：原本需要询问的操作会直接被拒绝。
        if self.mode is PermissionMode.DONT_ASK:
            return PermissionDecision(
                behavior=PermissionBehavior.DENY,
                message=(
                    f"{tool.definition.name} needs confirmation, "
                    "but prompts are disabled."
                ),
                decision_reason=PermissionDecisionReason(
                    PermissionDecisionKind.MODE, PermissionMode.DONT_ASK.value
                ),
            )

        # 普通工具局部 ask 与 passthrough 最终进入同一个 UI 边界，
        # 两者差异仍保留在可审计原因中。
        return PermissionDecision(
            behavior=PermissionBehavior.ASK,
            message=tool_result.message,
            decision_reason=tool_result.decision_reason,
            updated_input=tool_result.updated_input,
            suggestions=tool_result.suggestions,
        )

    async def decide_explicit_user_read(
        self, tool: Tool, tool_input: JsonObject, context: ToolContext
    ) -> PermissionDecision:
        """Authorize an explicit ``@path`` read without weakening any deny rule.

        The mention itself is the user's approval for this one read.  Input-level
        validation and safety checks still run, and both blanket and path-specific
        deny decisions remain authoritative.
        """

        deny_rule = self._whole_tool_rule(tool, PermissionBehavior.DENY)
        if deny_rule is not None:
            return PermissionDecision(
                PermissionBehavior.DENY,
                f"{tool.definition.name} is denied by an explicit rule.",
                _rule_reason(deny_rule),
            )
        tool_result = await tool.check_permissions(
            tool_input,
            ToolPermissionContext(
                mode=self.mode,
                rules=self.rules,
                tool_context=context,
            ),
        )
        if tool_result.behavior is ToolPermissionBehavior.DENY:
            return PermissionDecision(
                PermissionBehavior.DENY,
                tool_result.message,
                tool_result.decision_reason,
                updated_input=tool_result.updated_input,
            )
        return PermissionDecision(
            PermissionBehavior.ALLOW,
            "Allowed by the user's explicit @path mention.",
            PermissionDecisionReason(
                PermissionDecisionKind.USER, "explicit-file-mention"
            ),
            updated_input=_updated_input(tool_result.updated_input, tool_input),
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


def _rule_reason(rule: PermissionRule) -> PermissionDecisionReason:
    return PermissionDecisionReason(
        PermissionDecisionKind.RULE,
        f"{rule.tool_name}:{rule.behavior.value}",
        rule=rule,
    )
