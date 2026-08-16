"""一次 assistant 工具轮次的编排协议与现有执行器适配器。"""

import asyncio
from collections.abc import AsyncIterator, Callable

from nano_code.agent.contracts.tool import (
    ToolCallFinished as _ToolCallFinished,
)
from nano_code.agent.contracts.tool import (
    ToolCallStarted as _ToolCallStarted,
)
from nano_code.agent.contracts.tool import (
    ToolRoundCompleted as _ToolRoundCompleted,
)
from nano_code.agent.contracts.tool import (
    ToolRoundEvent as _ToolRoundEvent,
)
from nano_code.agent.ports.tool import ToolRoundPort
from nano_code.messages import ToolResultBlock, ToolUseBlock, TranscriptMessage
from nano_code.presentation import (
    ToolResultPresentation,
    ToolUsePresentation,
    compact_text,
)
from nano_code.tools.executor import ToolExecutionOutcome, ToolExecutor
from nano_code.tools.result_store import ToolResultStore


class ToolRoundExecutor(ToolRoundPort):
    """把现有 ToolExecutor 包装成 Agent-owned 工具轮次 port。

    调度策略刻意保留当前 MVP 的串行语义。以后增加并行调度时，只需要替换
    这个适配器，不必把取消补齐和展示投影逻辑重新放回 AgentEngine。
    """

    def __init__(
        self,
        executor: ToolExecutor,
        result_store_factory: Callable[[str], ToolResultStore] | None = None,
    ) -> None:
        self.executor = executor
        self._result_store_factory = result_store_factory

    @property
    def result_store(self) -> ToolResultStore:
        """兼容旧 composition/test helper 的只读结果存储访问。"""

        return self.executor.result_store

    def bind_result_store(self, result_store: ToolResultStore) -> None:
        """兼容旧调用方；新的 Agent-facing API 使用 ``bind_session``。"""

        self.executor.result_store = result_store

    def bind_session(self, session_id: str) -> None:
        """根据 session ID 切换工具结果存储。"""

        if self._result_store_factory is not None:
            self.executor.result_store = self._result_store_factory(session_id)

    def present_use(self, call: ToolUseBlock) -> ToolUsePresentation:
        return self.executor.present_use(call)

    def present_stored_result(
        self,
        call: ToolUseBlock,
        result: ToolResultBlock | None,
    ) -> ToolResultPresentation:
        return self.executor.present_stored_result(call, result)

    async def run_round(
        self,
        calls: tuple[ToolUseBlock, ...],
        assistant_message: TranscriptMessage,
    ) -> AsyncIterator[_ToolRoundEvent]:
        results: list[ToolResultBlock] = []
        try:
            # MVP 明确串行执行。每次调用完成后才开始下一个调用。
            for call in calls:
                yield _ToolCallStarted(call, self.present_use(call))
                try:
                    outcome = await self.executor.execute(call)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    # ToolExecutor 的正常失败已经会返回 result；这里覆盖适配器
                    # 外部注入的执行器异常，仍然保持协议闭合。
                    message = (
                        f"Unexpected {type(error).__name__} while executing {call.name}"
                    )
                    result = ToolResultBlock(
                        tool_use_id=call.id,
                        content=message,
                        is_error=True,
                        presentation=self.executor.present_error(call, message),
                    )
                    outcome = ToolExecutionOutcome(
                        result=result,
                        presentation=result.presentation
                        or ToolResultPresentation(summary=compact_text(message)),
                    )
                results.append(outcome.result)
                yield _ToolCallFinished(
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
                result = ToolResultBlock(
                    tool_use_id=call.id,
                    content=message,
                    is_error=True,
                    presentation=self.executor.present_error(call, message),
                )
                results.append(result)
                yield _ToolCallFinished(
                    call=call,
                    result=result,
                    presentation=result.presentation
                    or ToolResultPresentation(summary=compact_text(message)),
                )
            yield _ToolRoundCompleted(
                message=_tool_result_message(assistant_message, tuple(results)),
                results=tuple(results),
                cancelled=True,
            )
            raise

        yield _ToolRoundCompleted(
            message=_tool_result_message(assistant_message, tuple(results)),
            results=tuple(results),
        )


def _tool_result_message(
    assistant_message: TranscriptMessage,
    results: tuple[ToolResultBlock, ...],
) -> TranscriptMessage:
    if not results:
        raise ValueError("A tool round must contain at least one result")
    return TranscriptMessage(
        role="user",
        origin="tool",
        content=results,
        parent_uuid=assistant_message.uuid,
        source_message_uuid=assistant_message.uuid,
    )


__all__ = [
    "ToolRoundExecutor",
]
