"""统一的校验 → 权限 → 执行与双重结果投影管线。"""

import asyncio
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from my_code.conversation.attachments import AttachmentPayload
from my_code.conversation.models import ToolCall, ToolResult
from my_code.conversation.presentation import (
    ToolResultPresentation,
    generic_tool_result_presentation,
)
from my_code.foundation.json import JsonObject, to_json_object
from my_code.permissions.models import (
    PermissionBehavior,
    PermissionDecision,
    PermissionDecisionKind,
    PermissionDecisionReason,
    PermissionPrompt,
    PermissionPrompter,
    PermissionRequest,
    PermissionUpdate,
    PermissionUpdateDestination,
    ToolPermissionContext,
)
from my_code.permissions.policy import PermissionPolicy
from my_code.tools.base import (
    Tool,
    ToolContext,
    ToolExecutionError,
    ToolInputError,
    ToolOutput,
)
from my_code.tools.catalog import ToolCatalogSnapshot
from my_code.tools.invocation import (
    ToolInvocation,
    ToolInvocationAudit,
    ToolInvocationHook,
)
from my_code.tools.presentation import (
    ToolUsePresentation,
    generic_tool_use_presentation,
)
from my_code.workspace.local import Workspace

logger = logging.getLogger("my_code.permissions")


@dataclass(frozen=True, slots=True)
class ToolExecutionOutcome:
    """一次执行产生的完整结果和后续状态。"""

    result: ToolResult
    new_attachments: tuple[AttachmentPayload, ...] = ()
    permission_updates: tuple[PermissionUpdate, ...] = ()


class LoggingToolInvocationAudit:
    """Default structured permission audit sink."""

    async def record_permission(
        self,
        invocation: ToolInvocation,
        call: ToolCall,
        decision: object,
    ) -> None:
        reason = getattr(decision, "reason", "unknown")
        behavior = getattr(getattr(decision, "behavior", None), "value", "unknown")
        message = getattr(decision, "message", "")
        logger.info(
            "Permission decision: tool=%s origin=%s behavior=%s message=%s reason=%s",
            call.name,
            invocation.origin.value,
            behavior,
            message,
            reason,
        )


class ToolExecutor:
    """执行调用，并为每个常规失败保留一条工具结果。"""

    def __init__(
        self,
        tools: ToolCatalogSnapshot,
        policy: PermissionPolicy,
        prompter: PermissionPrompter,
        workspace: Workspace,
        update_applier: Callable[[tuple[PermissionUpdate, ...]], None] | None = None,
        hooks: Iterable[ToolInvocationHook] = (),
        audit: ToolInvocationAudit | None = None,
    ) -> None:
        self.tools = tools
        self.policy = policy
        self.prompter = prompter
        self.workspace = workspace
        self.context = ToolContext(workspace)
        self.update_applier = update_applier or _apply_updates(policy)
        self.hooks = tuple(hooks)
        self.audit = audit or LoggingToolInvocationAudit()
        self._permission_prompt_lock = asyncio.Lock()

    def present_use(
        self,
        call: ToolCall,
        *,
        tools: ToolCatalogSnapshot | None = None,
    ) -> ToolUsePresentation:
        """请求 Tool 解释调用语义；未知或异常工具使用安全回退。"""

        active_tools = self.tools if tools is None else tools
        tool = active_tools.get(call.name)
        tool_input = to_json_object(call.input)
        if tool is not None:
            try:
                return tool.present_use(tool_input)
            except Exception:
                # 展示扩展不是安全边界，故障不能阻止工具进入校验和权限管线。
                pass
        return generic_tool_use_presentation(call.name, tool_input)

    def present_error(
        self,
        call: ToolCall,
        message: str,
        *,
        tools: ToolCatalogSnapshot | None = None,
    ) -> ToolResultPresentation:
        """请求 Tool 展示错误；未知或异常工具使用安全回退。"""

        active_tools = self.tools if tools is None else tools
        tool = active_tools.get(call.name)
        tool_input = to_json_object(call.input)
        if tool is not None:
            try:
                return tool.present_error(tool_input, message)
            except Exception:
                pass
        return generic_tool_result_presentation(message, True)

    async def execute(
        self,
        call: ToolCall,
        *,
        tools: ToolCatalogSnapshot | None = None,
        invocation: ToolInvocation | None = None,
        run_id: str | None = None,
    ) -> ToolExecutionOutcome:
        actual_invocation = invocation or ToolInvocation()
        submitted_call = _copy_call(call)
        submitted_input = submitted_call.input
        active_tools = self.tools if tools is None else tools
        execution_context = self.context.with_tools(
            active_tools.as_mapping(),
            version=active_tools.version,
            run_id=run_id,
        )
        tool = active_tools.get(submitted_call.name)
        if tool is None:
            # 未知工具名以协议结果形式报告给模型，而不是中断整个智能体循环。
            return self._error(submitted_call, f"Unknown tool: {submitted_call.name}")

        # 校验必须先于权限检查：绝不能请求用户批准格式错误或语义含混的输入。
        try:
            tool.validate_input(to_json_object(submitted_input))
        except (ToolInputError, ValueError, TypeError) as error:
            return self._error(submitted_call, f"Invalid input: {error}", tool=tool)

        # 权限是独立策略层；只有静态策略及所需用户确认均通过后，才调用 Tool.execute。
        try:
            tool_result = await tool.check_permissions(
                to_json_object(submitted_input),
                ToolPermissionContext(
                    mode=self.policy.mode,
                    rules=self.policy.rules,
                    workspace_root=self.workspace.root,
                ),
            )
            decision = self.policy.decide(
                PermissionRequest(
                    tool_name=tool.definition.name,
                    tool_input=to_json_object(submitted_input),
                    tool_result=tool_result,
                )
            )
        except Exception as error:
            return self._error(
                submitted_call,
                f"Unexpected {type(error).__name__} while checking permissions",
                tool=tool,
            )

        # 工具专属权限检查可能规范化或约束输入。此后只使用这一份深拷贝，
        # audit、prompter 和 hook 都只能接触各自的隔离副本。
        approved_input = to_json_object(
            submitted_input
            if decision.updated_input is None
            else decision.updated_input
        )
        if decision.behavior is not PermissionBehavior.DENY:
            try:
                tool.validate_input(to_json_object(approved_input))
            except (ToolInputError, ValueError, TypeError) as error:
                return self._error(
                    submitted_call,
                    f"Invalid approved input: {error}",
                    tool=tool,
                    tool_input=approved_input,
                )
        try:
            await self.audit.record_permission(
                actual_invocation,
                _copy_call(submitted_call),
                _copy_decision(decision),
            )
        except Exception:
            return self._error(
                submitted_call,
                "Tool audit failed; the tool was not executed.",
                tool=tool,
                tool_input=approved_input,
            )
        if decision.behavior is PermissionBehavior.DENY:
            return self._error(
                submitted_call,
                f"Permission denied: {decision.message}",
                tool=tool,
            )
        if decision.behavior is PermissionBehavior.ASK:
            permission_input = approved_input
            try:
                permission_call = _copy_call(
                    submitted_call, tool_input=permission_input
                )
                presentation = self.present_use(permission_call)
                async with self._permission_prompt_lock:
                    confirmation = await self.prompter.confirm(
                        PermissionPrompt(
                            tool_name=tool.definition.name,
                            tool_input=to_json_object(permission_input),
                            decision=_copy_decision(decision),
                            display_name=presentation.display_name,
                            summary=presentation.summary,
                            activity=presentation.activity,
                        )
                    )
            except Exception as error:
                return self._error(
                    submitted_call,
                    f"Permission prompt failed ({type(error).__name__}); "
                    "the tool was not executed.",
                    tool=tool,
                    tool_input=permission_input,
                )
            if not confirmation.allowed:
                feedback = (
                    f" User feedback: {confirmation.feedback}"
                    if confirmation.feedback is not None
                    else ""
                )
                denied = PermissionDecision(
                    PermissionBehavior.DENY,
                    "approval was not provided.",
                    PermissionDecisionReason(
                        PermissionDecisionKind.USER, "interactive-denial"
                    ),
                    updated_input=permission_input,
                )
                try:
                    await self.audit.record_permission(
                        actual_invocation,
                        _copy_call(submitted_call),
                        _copy_decision(denied),
                    )
                except Exception:
                    logger.exception(
                        "Permission denial audit failed: tool=%s origin=%s",
                        call.name,
                        actual_invocation.origin.value,
                    )
                return self._error(
                    submitted_call,
                    "Permission denied: approval was not provided. "
                    f"Reason: {decision.reason}.{feedback}",
                    tool=tool,
                    tool_input=permission_input,
                )
            if confirmation.updates:
                try:
                    self.update_applier(confirmation.updates)
                except Exception as error:
                    logger.warning(
                        "Permission update failed: tool=%s error=%s",
                        tool.definition.name,
                        type(error).__name__,
                    )
                    return self._error(
                        submitted_call,
                        "Permission update failed; the tool was not executed.",
                        tool=tool,
                        tool_input=permission_input,
                    )
            approved = PermissionDecision(
                PermissionBehavior.ALLOW,
                "Approved by the user.",
                PermissionDecisionReason(
                    PermissionDecisionKind.USER, "interactive-approval"
                ),
                updated_input=permission_input,
            )
            try:
                await self.audit.record_permission(
                    actual_invocation,
                    _copy_call(submitted_call),
                    _copy_decision(approved),
                )
            except Exception:
                return self._error(
                    submitted_call,
                    "Tool audit failed; the tool was not executed.",
                    tool=tool,
                    tool_input=permission_input,
                )

        try:
            for hook in self.hooks:
                await hook.before_execute(
                    actual_invocation,
                    _copy_call(submitted_call, tool_input=approved_input),
                    tool,
                    to_json_object(approved_input),
                    execution_context,
                )
            output = await tool.execute(
                to_json_object(approved_input), execution_context
            )

            # Tool 分别决定用户展示语义和模型序列化；TUI 与 Executor 均不反向
            # 解析模型可见字符串来猜测结果含义。
            model_content = tool.to_model_result(output)
            presentation = self._present_result(tool, approved_input, output)

            result = ToolResult(
                tool_use_id=submitted_call.id,
                content=model_content,
                presentation=presentation,
                is_error=output.is_error,
            )
            for hook in self.hooks:
                try:
                    await hook.after_execute(
                        actual_invocation,
                        _copy_call(submitted_call, tool_input=approved_input),
                        result,
                    )
                except Exception:
                    logger.exception(
                        "Post-tool hook failed after tool completion: "
                        "tool=%s origin=%s",
                        call.name,
                        actual_invocation.origin.value,
                    )
            return ToolExecutionOutcome(
                result,
                output.new_attachments,
                output.permission_updates,
            )
        except (ToolInputError, ToolExecutionError, OSError, UnicodeError) as error:
            return self._error(
                submitted_call,
                f"{type(error).__name__}: {error}",
                tool=tool,
                tool_input=approved_input,
            )
        except Exception as error:
            # 意外异常文本可能包含凭据或实现细节，只向模型保留稳定的异常类名。
            return self._error(
                submitted_call,
                "Unexpected "
                f"{type(error).__name__} while executing {submitted_call.name}",
                tool=tool,
                tool_input=approved_input,
            )

    @staticmethod
    def _present_result(
        tool: Tool,
        tool_input: JsonObject,
        output: ToolOutput,
    ) -> ToolResultPresentation:
        try:
            return tool.present_result(to_json_object(tool_input), output)
        except Exception:
            # Tool 已经执行成功；展示层错误不能把成功改写成失败并诱发模型重试。
            return generic_tool_result_presentation(output.content, output.is_error)

    @staticmethod
    def _error(
        call: ToolCall,
        message: str,
        *,
        tool: Tool | None = None,
        tool_input: JsonObject | None = None,
    ) -> ToolExecutionOutcome:
        # 必须保留原始 ID：provider 会拒绝包含 tool_use 却没有匹配 tool_result 的历史。
        actual_input = to_json_object(call.input if tool_input is None else tool_input)
        try:
            presentation = (
                tool.present_error(actual_input, message)
                if tool is not None
                else generic_tool_result_presentation(message, True)
            )
        except Exception:
            presentation = generic_tool_result_presentation(message, True)
        result = ToolResult(
            tool_use_id=call.id,
            content=message,
            presentation=presentation,
            is_error=True,
        )
        return ToolExecutionOutcome(result)

    def apply_session_updates(self, updates: tuple[PermissionUpdate, ...]) -> None:
        if any(
            update.destination is not PermissionUpdateDestination.SESSION
            for update in updates
        ):
            raise ValueError("Tool follow-ups may update session permissions only")
        self.update_applier(updates)


def _copy_call(
    call: ToolCall,
    *,
    tool_input: JsonObject | None = None,
) -> ToolCall:
    return ToolCall(
        id=call.id,
        name=call.name,
        input=to_json_object(call.input if tool_input is None else tool_input),
    )


def _copy_decision(decision: PermissionDecision) -> PermissionDecision:
    return PermissionDecision(
        behavior=decision.behavior,
        message=decision.message,
        decision_reason=decision.decision_reason,
        updated_input=(
            None
            if decision.updated_input is None
            else to_json_object(decision.updated_input)
        ),
        suggestions=decision.suggestions,
    )


def _apply_updates(
    policy: PermissionPolicy,
) -> Callable[[tuple[PermissionUpdate, ...]], None]:
    def apply(updates: tuple[PermissionUpdate, ...]) -> None:
        for update in updates:
            policy.apply_update(update)

    return apply


__all__ = [
    "ToolExecutor",
]
