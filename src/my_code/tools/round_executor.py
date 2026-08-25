"""一次 ToolRound 的事件与串行执行。"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

from my_code.conversation.models import (
    AssistantMessage,
    ToolCall,
    ToolResult,
    ToolResultBatch,
)
from my_code.tools.catalog import ToolCatalogSnapshot
from my_code.tools.executor import ToolExecutionOutcome, ToolExecutor
from my_code.tools.presentation import (
    ToolResultPresentation,
    ToolUsePresentation,
)


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
    message: ToolResultBatch
    cancelled: bool = False


type ToolRoundEvent = ToolCallStarted | ToolCallFinished | ToolRoundCompleted


class ToolRoundExecutor:
    """Execute safe call groups concurrently and close every protocol result."""

    def __init__(
        self,
        executor: ToolExecutor,
        *,
        max_parallel_calls: int = 1,
    ) -> None:
        if max_parallel_calls < 1:
            raise ValueError("max_parallel_calls must be positive")
        self.executor = executor
        self.max_parallel_calls = max_parallel_calls

    async def run_round(
        self,
        calls: tuple[ToolCall, ...],
        assistant_message: AssistantMessage,
        *,
        tools: ToolCatalogSnapshot | None = None,
        run_id: str | None = None,
    ) -> AsyncIterator[ToolRoundEvent]:
        active_tools = self.executor.tools if tools is None else tools
        results: list[ToolResult] = []
        try:
            for group in _execution_groups(calls, active_tools):
                for call in group:
                    yield ToolCallStarted(
                        call,
                        self.executor.present_use(call, tools=active_tools),
                    )
                outcomes = await self._execute_group(
                    group,
                    active_tools,
                    run_id=run_id,
                )
                for call, outcome in zip(group, outcomes, strict=True):
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
                presentation = self.executor.present_error(
                    call, message, tools=active_tools
                )
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

    async def _execute_group(
        self,
        calls: tuple[ToolCall, ...],
        tools: ToolCatalogSnapshot,
        *,
        run_id: str | None,
    ) -> tuple[ToolExecutionOutcome, ...]:
        semaphore = asyncio.Semaphore(self.max_parallel_calls)

        async def execute(call: ToolCall) -> ToolExecutionOutcome:
            async with semaphore:
                try:
                    return await self.executor.execute(
                        call,
                        tools=tools,
                        run_id=run_id,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    # ToolExecutor 的正常失败已经会返回 result；这里覆盖适配器
                    # 外部注入的执行器异常，仍然保持协议闭合。
                    message = (
                        f"Unexpected {type(error).__name__} while executing {call.name}"
                    )
                    return ToolExecutionOutcome(
                        result=ToolResult(
                            tool_use_id=call.id,
                            content=message,
                            is_error=True,
                        ),
                        presentation=self.executor.present_error(
                            call, message, tools=tools
                        ),
                    )

        tasks = tuple(asyncio.create_task(execute(call)) for call in calls)
        try:
            return tuple(await asyncio.gather(*tasks))
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise


def _execution_groups(
    calls: tuple[ToolCall, ...],
    tools: ToolCatalogSnapshot,
) -> tuple[tuple[ToolCall, ...], ...]:
    groups: list[tuple[ToolCall, ...]] = []
    safe: list[ToolCall] = []
    for call in calls:
        tool = tools.get(call.name)
        try:
            concurrency_safe = tool is not None and tool.is_concurrency_safe(call.input)
        except Exception:
            concurrency_safe = False
        if concurrency_safe:
            safe.append(call)
            continue
        if safe:
            groups.append(tuple(safe))
            safe = []
        groups.append((call,))
    if safe:
        groups.append(tuple(safe))
    return tuple(groups)


def _tool_result_message(
    assistant_message: AssistantMessage,
    results: tuple[ToolResult, ...],
) -> ToolResultBatch:
    if not results:
        raise ValueError("A tool round must contain at least one result")
    return ToolResultBatch(
        content=results,
        parent_uuid=assistant_message.uuid,
        source_assistant_id=assistant_message.uuid,
    )


__all__ = [
    "ToolCallFinished",
    "ToolCallStarted",
    "ToolRoundCompleted",
    "ToolRoundEvent",
    "ToolRoundExecutor",
]
