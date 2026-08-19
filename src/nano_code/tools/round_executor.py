"""一次 ToolRound 的事件与串行执行。"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

from nano_code.conversation import (
    AssistantMessage,
    ToolCall,
    ToolResult,
    ToolResultsMessage,
)
from nano_code.tools.executor import ToolExecutionOutcome, ToolExecutor
from nano_code.tools.presentation import (
    ToolResultPresentation,
    ToolUsePresentation,
)
from nano_code.tools.result_store import ToolResultStore


@dataclass(frozen=True, slots=True)
class ToolCallStarted:
    call: ToolCall
    presentation: ToolUsePresentation


@dataclass(frozen=True, slots=True)
class ToolCallFinished:
    call: ToolCall
    result: ToolResult
    presentation: ToolResultPresentation


@dataclass(frozen=True, slots=True)
class ToolRoundCompleted:
    message: ToolResultsMessage
    cancelled: bool = False


type ToolRoundEvent = ToolCallStarted | ToolCallFinished | ToolRoundCompleted


class ToolRoundExecutor:
    """串行执行一组 ToolCall 并保证每个调用都有闭合结果。

    调度策略刻意保留当前 MVP 的串行语义；取消补齐和展示投影不泄漏到 Agent。
    """

    def __init__(
        self,
        executor: ToolExecutor,
    ) -> None:
        self.executor = executor

    def present_use(self, call: ToolCall) -> ToolUsePresentation:
        return self.executor.present_use(call)

    def present_stored_result(
        self,
        call: ToolCall,
        result: ToolResult | None,
    ) -> ToolResultPresentation:
        return self.executor.present_stored_result(call, result)

    async def run_round(
        self,
        calls: tuple[ToolCall, ...],
        assistant_message: AssistantMessage,
        *,
        result_store: ToolResultStore,
    ) -> AsyncIterator[ToolRoundEvent]:
        results: list[ToolResult] = []
        try:
            # MVP 明确串行执行。每次调用完成后才开始下一个调用。
            for call in calls:
                yield ToolCallStarted(call, self.present_use(call))
                try:
                    outcome = await self.executor.execute(
                        call, result_store=result_store
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    # ToolExecutor 的正常失败已经会返回 result；这里覆盖适配器
                    # 外部注入的执行器异常，仍然保持协议闭合。
                    message = (
                        f"Unexpected {type(error).__name__} while executing {call.name}"
                    )
                    result = ToolResult(
                        tool_use_id=call.id,
                        content=message,
                        is_error=True,
                    )
                    presentation = self.executor.present_error(call, message)
                    outcome = ToolExecutionOutcome(
                        result=result,
                        presentation=presentation,
                    )
                results.append(outcome.result)
                yield ToolCallFinished(
                    call=call,
                    result=outcome.result,
                    presentation=outcome.presentation,
                )
        except asyncio.CancelledError:
            # 对尚未产生结果的调用逐一补齐稳定错误。先发出事件，Agent 可以
            # 在重新抛出取消前把整条 user tool-result 消息持久化。
            completed_ids = {result.tool_use_id for result in results}
            message = "Tool execution was cancelled."
            for call in calls:
                if call.id in completed_ids:
                    continue
                result = ToolResult(
                    tool_use_id=call.id,
                    content=message,
                    is_error=True,
                )
                presentation = self.executor.present_error(call, message)
                results.append(result)
                yield ToolCallFinished(
                    call=call,
                    result=result,
                    presentation=presentation,
                )
            yield ToolRoundCompleted(
                message=_tool_result_message(assistant_message, tuple(results)),
                cancelled=True,
            )
            raise

        yield ToolRoundCompleted(
            message=_tool_result_message(assistant_message, tuple(results)),
        )


def _tool_result_message(
    assistant_message: AssistantMessage,
    results: tuple[ToolResult, ...],
) -> ToolResultsMessage:
    if not results:
        raise ValueError("A tool round must contain at least one result")
    return ToolResultsMessage(
        content=results,
        parent_uuid=assistant_message.uuid,
        source_assistant_uuid=assistant_message.uuid,
    )


__all__ = [
    "ToolCallFinished",
    "ToolCallStarted",
    "ToolRoundCompleted",
    "ToolRoundEvent",
    "ToolRoundExecutor",
]
