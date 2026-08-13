"""单个持久化用户轮次中的模型 → 工具 → 模型循环。"""

import asyncio
from collections.abc import AsyncIterator

from nano_code.agent.engine_types import AgentTurnResult
from nano_code.agent.events import (
    AgentEvent,
    AgentTextDelta,
    AgentToolFinished,
    AgentToolStarted,
    AgentTurnCompleted,
)
from nano_code.context import ContextWindow
from nano_code.messages import (
    ChatMessage,
    TextBlock,
    TokenUsage,
    ToolResultBlock,
    ToolUseBlock,
)
from nano_code.providers import (
    ModelProvider,
    ModelRequest,
    ModelResponseCompleted,
    ModelTextDelta,
    StreamingModelProvider,
)
from nano_code.sessions import SessionStore
from nano_code.tools.executor import ToolExecutor


class AgentEngine:
    """管理消息历史，并将 provider、工具和持久化职责委托给对应组件。"""

    def __init__(
        self,
        provider: ModelProvider,
        tool_executor: ToolExecutor,
        session_store: SessionStore,
        context_window: ContextWindow,
        system_prompt: str,
        *,
        max_turns: int = 12,
        max_output_tokens: int = 8192,
    ) -> None:
        if max_turns < 1:
            raise ValueError("max_turns must be positive")
        self.provider = provider
        self.tool_executor = tool_executor
        self.session_store = session_store
        self.context_window = context_window
        self.system_prompt = system_prompt
        self.max_turns = max_turns
        self.max_output_tokens = max_output_tokens

        # 会话记录是持久化事实来源。接受新输入前先修复中断的协议轮次，
        # 避免恢复时携带 provider 会拒绝的孤立 tool_use。
        self.messages = list(session_store.load())
        self._repair_trailing_tool_uses()

    def resume(self, session_store: SessionStore) -> tuple[ChatMessage, ...]:
        """校验并恢复另一会话；失败时保持当前引擎状态不变。"""

        loaded = list(session_store.load())
        if not loaded:
            raise ValueError(
                f"Session contains no messages: {session_store.session_id}"
            )
        repairs = _trailing_tool_repairs(loaded)
        if repairs is not None:
            repair_message = ChatMessage(
                role="user",
                origin="tool",
                content=repairs,
                parent_uuid=loaded[-1].uuid,
                source_message_uuid=loaded[-1].uuid,
            )
            # 先完成目标会话的恢复写入，再切换内存状态，确保失败不会留下
            # 一半属于旧会话、一半属于新会话的引擎。
            session_store.append(repair_message)
            loaded.append(repair_message)

        self.session_store = session_store
        self.messages = loaded
        return tuple(loaded)

    async def submit(self, prompt: str) -> AgentTurnResult:
        """消费可观察循环并返回终态值。"""

        completed: AgentTurnResult | None = None
        async for event in self.submit_stream(prompt):
            if isinstance(event, AgentTurnCompleted):
                completed = event.result
        if completed is None:
            raise RuntimeError("Agent stream ended without a completed turn")
        return completed

    async def submit_stream(self, prompt: str) -> AsyncIterator[AgentEvent]:
        """运行一个轮次，同时暴露临时文本和工具生命周期事件。"""

        if not prompt.strip():
            raise ValueError("Prompt must not be empty")

        user_message = ChatMessage(
            role="user",
            origin="human",
            content=(TextBlock(prompt),),
            parent_uuid=self._last_uuid,
        )

        # 首次 API 请求前先持久化；崩溃或网络故障不应抹去发起轮次的提示。
        self._append(user_message)

        input_tokens = 0
        output_tokens = 0
        final_text = ""
        for turn in range(1, self.max_turns + 1):
            # 保留完整会话记录用于恢复，但每次迭代都独立生成有界 API 视图。
            projected = self.context_window.project(tuple(self.messages))
            request = ModelRequest(
                system_prompt=self.system_prompt,
                messages=projected,
                tools=self.tool_executor.registry.definitions,
                max_output_tokens=self.max_output_tokens,
            )
            response = None
            streamed_text = False
            if isinstance(self.provider, StreamingModelProvider):
                async for event in self.provider.stream(request):
                    if isinstance(event, ModelTextDelta):
                        streamed_text = True
                        yield AgentTextDelta(event.text)
                    elif isinstance(event, ModelResponseCompleted):
                        response = event.response
            else:
                response = await self.provider.complete(request)
            if response is None:
                raise RuntimeError("Provider stream ended without a final response")
            if not streamed_text:
                for block in response.content:
                    if isinstance(block, TextBlock):
                        yield AgentTextDelta(block.text)
            input_tokens += response.usage.input_tokens
            output_tokens += response.usage.output_tokens

            assistant_message = ChatMessage(
                role="assistant",
                origin="model",
                content=response.content,
                parent_uuid=self._last_uuid,
            )

            # 执行前先存储模型的工具请求，使中断过程可观察，并能在下次恢复时修复。
            self._append(assistant_message)
            final_text = "\n".join(
                block.text for block in response.content if isinstance(block, TextBlock)
            ).strip()
            tool_calls = tuple(
                block for block in response.content if isinstance(block, ToolUseBlock)
            )

            # 没有工具调用是当前 MVP 明确的终止条件。
            if not tool_calls:
                turn_result = AgentTurnResult(
                    text=final_text,
                    turns=turn,
                    usage=TokenUsage(input_tokens, output_tokens),
                )
                yield AgentTurnCompleted(turn_result)
                return

            results: list[ToolResultBlock] = []
            try:
                # MVP 刻意串行执行调用。未来并行执行必须遵守各工具的并发契约
                # 与上下文变更契约。
                for call in tool_calls:
                    yield AgentToolStarted(call.id, call.name, call.input)
                    tool_result = await self.tool_executor.execute(call)
                    results.append(tool_result)
                    yield AgentToolFinished(
                        call.id,
                        call.name,
                        tool_result.content,
                        tool_result.is_error,
                    )
            except asyncio.CancelledError:
                # Anthropic 协议要求每个已发出的 tool_use 都有对应结果，
                # 包括因轮次取消而尚未开始的调用。
                completed_ids = {result.tool_use_id for result in results}
                results.extend(
                    ToolResultBlock(
                        tool_use_id=call.id,
                        content="Tool execution was cancelled.",
                        is_error=True,
                    )
                    for call in tool_calls
                    if call.id not in completed_ids
                )
                self._append_tool_results(results, assistant_message.uuid)
                raise
            self._append_tool_results(results, assistant_message.uuid)

        raise RuntimeError(
            f"Agent reached max_turns={self.max_turns} after the last tool result"
        )

    @property
    def _last_uuid(self) -> str | None:
        return self.messages[-1].uuid if self.messages else None

    def _append(self, message: ChatMessage) -> None:
        # 持久化优先的顺序可防止写入失败时内存历史领先于会话记录。
        self.session_store.append(message)
        self.messages.append(message)

    def _append_tool_results(
        self, results: list[ToolResultBlock], assistant_uuid: str
    ) -> None:
        # 将一次模型响应的所有结果放在同一条 user 角色消息中。
        # 在 tool_use 与 tool_result 之间插入普通用户文本不符合协议。
        tool_message = ChatMessage(
            role="user",
            origin="tool",
            content=tuple(results),
            parent_uuid=self._last_uuid,
            source_message_uuid=assistant_uuid,
        )
        self._append(tool_message)

    def _repair_trailing_tool_uses(self) -> None:
        """闭合上次进程退出时遗留的不完整工具协议轮次。"""

        repairs = _trailing_tool_repairs(self.messages)
        if repairs is None:
            return
        # 合成错误比重放可能有副作用的工具更安全：上个进程可能在退出前已完成副作用。
        self._append_tool_results(list(repairs), self.messages[-1].uuid)


def _trailing_tool_repairs(
    messages: list[ChatMessage],
) -> tuple[ToolResultBlock, ...] | None:
    """为末尾未闭合的工具请求构造协议修复结果。"""

    if not messages:
        return None
    trailing = messages[-1]
    if trailing.role != "assistant":
        return None
    calls = tuple(
        block for block in trailing.content if isinstance(block, ToolUseBlock)
    )
    if not calls:
        return None
    return tuple(
        ToolResultBlock(
            tool_use_id=call.id,
            content="Tool execution was interrupted before the session resumed.",
            is_error=True,
        )
        for call in calls
    )
