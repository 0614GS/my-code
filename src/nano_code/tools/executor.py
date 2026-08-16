"""统一的校验 → 权限 → 执行与双重结果投影管线。"""

from dataclasses import dataclass

from nano_code.messages import JsonObject, ToolCall, ToolResult
from nano_code.permissions import PermissionBehavior, PermissionPolicy
from nano_code.permissions.prompt import PermissionPrompter
from nano_code.presentation import (
    ToolResultPresentation,
    ToolUsePresentation,
    compact_text,
    generic_tool_use_presentation,
)
from nano_code.tools.base import (
    Tool,
    ToolContext,
    ToolExecutionError,
    ToolInputError,
    ToolOutput,
)
from nano_code.tools.registry import ToolRegistry
from nano_code.tools.result_store import ToolResultStore


@dataclass(frozen=True, slots=True)
class ToolExecutionOutcome:
    """一次执行产生的模型结果和用户展示结果。"""

    result: ToolResult
    presentation: ToolResultPresentation


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

    def present_use(self, call: ToolCall) -> ToolUsePresentation:
        """请求 Tool 解释调用语义；未知或异常工具使用安全回退。"""

        tool = self.registry.get(call.name)
        if tool is not None:
            try:
                return tool.present_use(call.input)
            except Exception:
                # 展示扩展不是安全边界，故障不能阻止工具进入校验和权限管线。
                pass
        return generic_tool_use_presentation(call.name, call.input)

    def present_error(self, call: ToolCall, message: str) -> ToolResultPresentation:
        """请求 Tool 展示错误；未知或异常工具使用安全回退。"""

        tool = self.registry.get(call.name)
        if tool is not None:
            try:
                return tool.present_error(call.input, message)
            except Exception:
                pass
        return ToolResultPresentation(summary=compact_text(message))

    def present_stored_result(
        self,
        call: ToolCall,
        result: ToolResult | None,
    ) -> ToolResultPresentation:
        """投影历史结果，并兼容尚未保存展示快照的旧 Transcript。"""

        if result is None:
            return self.present_error(
                call, "Tool result is missing from the transcript."
            )
        if result.presentation is not None:
            return result.presentation
        tool = self.registry.get(call.name)
        if tool is None or result.is_error:
            return self.present_error(call, result.content)
        return self._present_result(
            tool,
            call.input,
            ToolOutput(content=result.content, is_error=False),
        )

    async def execute(self, call: ToolCall) -> ToolExecutionOutcome:
        tool = self.registry.get(call.name)
        if tool is None:
            # 未知工具名以协议结果形式报告给模型，而不是中断整个智能体循环。
            return self._error(call, f"Unknown tool: {call.name}")

        # 校验必须先于权限检查：绝不能请求用户批准格式错误或语义含混的输入。
        try:
            tool.validate_input(call.input)
        except (ToolInputError, ValueError, TypeError) as error:
            return self._error(call, f"Invalid input: {error}", tool=tool)

        # 权限是独立策略层；只有静态策略及所需用户确认均通过后，才调用 Tool.execute。
        decision = await self.policy.decide(tool, call.input, self.context)
        if decision.behavior is PermissionBehavior.DENY:
            return self._error(
                call, f"Permission denied: {decision.message}", tool=tool
            )
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
                    tool=tool,
                    tool_input=permission_input,
                )

        try:
            # 工具专属权限检查可能规范化或约束输入；执行阶段必须使用获准的准确输入。
            approved_input = (
                call.input if decision.updated_input is None else decision.updated_input
            )
            output = await tool.execute(approved_input, self.context)

            # Tool 分别决定用户展示语义和模型序列化；TUI 与 Executor 均不反向
            # 解析模型可见字符串来猜测结果含义。
            model_content = tool.to_model_result(output)
            presentation = self._present_result(tool, approved_input, output)

            # 构造 API 块前先外置结果，使后续每一层看到相同、有界且可重放的内容。
            content = self.result_store.externalize(call.id, model_content)
            result = ToolResult(
                tool_use_id=call.id,
                content=content,
                is_error=output.is_error,
                presentation=presentation,
            )
            return ToolExecutionOutcome(result, presentation)
        except (ToolInputError, ToolExecutionError, OSError, UnicodeError) as error:
            return self._error(call, f"{type(error).__name__}: {error}", tool=tool)
        except Exception as error:
            # 意外异常文本可能包含凭据或实现细节，只向模型保留稳定的异常类名。
            return self._error(
                call,
                f"Unexpected {type(error).__name__} while executing {call.name}",
                tool=tool,
            )

    @staticmethod
    def _present_result(
        tool: Tool,
        tool_input: JsonObject,
        output: ToolOutput,
    ) -> ToolResultPresentation:
        try:
            return tool.present_result(tool_input, output)
        except Exception:
            # Tool 已经执行成功；展示层错误不能把成功改写成失败并诱发模型重试。
            return Tool.present_result(tool, tool_input, output)

    @staticmethod
    def _error(
        call: ToolCall,
        message: str,
        *,
        tool: Tool | None = None,
        tool_input: JsonObject | None = None,
    ) -> ToolExecutionOutcome:
        # 必须保留原始 ID：provider 会拒绝包含 tool_use 却没有匹配 tool_result 的历史。
        actual_input = call.input if tool_input is None else tool_input
        try:
            presentation = (
                tool.present_error(actual_input, message)
                if tool is not None
                else ToolResultPresentation(summary=compact_text(message))
            )
        except Exception:
            presentation = ToolResultPresentation(summary=compact_text(message))
        result = ToolResult(
            tool_use_id=call.id,
            content=message,
            is_error=True,
            presentation=presentation,
        )
        return ToolExecutionOutcome(result, presentation)
