"""统一的校验 → 权限 → 执行管线。"""

from nano_code.messages import ToolResultBlock, ToolUseBlock
from nano_code.permissions import PermissionBehavior, PermissionPolicy
from nano_code.permissions.prompt import PermissionPrompter
from nano_code.tools.base import (
    ToolContext,
    ToolExecutionError,
    ToolInputError,
)
from nano_code.tools.registry import ToolRegistry
from nano_code.tools.result_store import ToolResultStore


class ToolExecutor:
    """执行调用，并为每个常规失败保留一条工具结果。"""

    def __init__(
        self,
        registry: ToolRegistry,
        policy: PermissionPolicy,
        prompter: PermissionPrompter,
        context: ToolContext,
        result_store: ToolResultStore,
    ) -> None:
        self.registry = registry
        self.policy = policy
        self.prompter = prompter
        self.context = context
        self.result_store = result_store

    async def execute(self, call: ToolUseBlock) -> ToolResultBlock:
        tool = self.registry.get(call.name)
        if tool is None:
            # 未知工具名以协议结果形式报告给模型，而不是中断整个智能体循环。
            return self._error(call, f"Unknown tool: {call.name}")

        # 校验必须先于权限检查：绝不能请求用户批准格式错误或语义含混的输入。
        try:
            tool.validate_input(call.input)
        except (ToolInputError, ValueError, TypeError) as error:
            return self._error(call, f"Invalid input: {error}")

        # 权限是独立策略层；只有静态策略及所需用户确认均通过后，才调用 Tool.execute。
        decision = await self.policy.decide(tool, call.input, self.context)
        if decision.behavior is PermissionBehavior.DENY:
            return self._error(call, f"Permission denied: {decision.message}")
        if decision.behavior is PermissionBehavior.ASK:
            permission_input = (
                call.input if decision.updated_input is None else decision.updated_input
            )
            confirmation = await self.prompter.confirm(tool, permission_input, decision)
            if not confirmation.allowed:
                feedback = (
                    f" User feedback: {confirmation.feedback}"
                    if confirmation.feedback is not None
                    else ""
                )
                return self._error(
                    call,
                    "Permission denied: approval was not provided. "
                    f"Reason: {decision.reason}.{feedback}",
                )

        try:
            # 工具专属权限检查可能规范化或约束输入；执行阶段必须使用获准的准确输入。
            approved_input = (
                call.input if decision.updated_input is None else decision.updated_input
            )
            output = await tool.execute(approved_input, self.context)

            # 构造 API 块前先外置结果，使后续每一层看到相同、有界且可重放的内容。
            content = self.result_store.externalize(call.id, output.content)
            return ToolResultBlock(
                tool_use_id=call.id,
                content=content,
                is_error=output.is_error,
            )
        except (ToolInputError, ToolExecutionError, OSError, UnicodeError) as error:
            return self._error(call, f"{type(error).__name__}: {error}")
        except Exception as error:
            # 意外异常文本可能包含凭据或实现细节，只向模型保留稳定的异常类名。
            return self._error(
                call, f"Unexpected {type(error).__name__} while executing {call.name}"
            )

    @staticmethod
    def _error(call: ToolUseBlock, message: str) -> ToolResultBlock:
        # 必须保留原始 ID：provider 会拒绝包含 tool_use 却没有匹配 tool_result 的历史。
        return ToolResultBlock(
            tool_use_id=call.id,
            content=message,
            is_error=True,
        )
