"""单个持久化用户轮次中的模型 → 工具 → 模型状态机。"""

import asyncio
from collections.abc import AsyncIterator

from nano_code.agent.contracts.context import ContextPlan
from nano_code.agent.contracts.inbound import (
    AgentContextState,
    AgentHistoryAssistantMessage,
    AgentHistorySystemMessage,
    AgentHistoryToolCall,
    AgentHistoryUserMessage,
    AgentSessionView,
    AgentState,
    AgentTurnResult,
)
from nano_code.agent.contracts.model import (
    ModelResponseCompleted,
    ModelTextDelta,
)
from nano_code.agent.contracts.session import CompactBoundary, CompactTrigger
from nano_code.agent.contracts.tool import (
    ToolCallFinished,
    ToolCallStarted,
    ToolRoundCompleted,
)
from nano_code.agent.conversation import ConversationState
from nano_code.agent.errors import ContextOverflow, ModelContextOverflow
from nano_code.agent.events import (
    AgentEvent,
    AgentTextDelta,
    AgentToolFinished,
    AgentToolStarted,
    AgentTurnCompleted,
)
from nano_code.agent.ports.compaction import CompactorPort
from nano_code.agent.ports.context import ContextPort
from nano_code.agent.ports.inbound import AgentInboundPort
from nano_code.agent.ports.model import ModelTurnPort
from nano_code.agent.ports.session import SessionRepository
from nano_code.agent.ports.tool import ToolRoundPort
from nano_code.messages import (
    ModelResponse,
    TextBlock,
    TokenUsage,
    ToolResultBlock,
    ToolUseBlock,
    TranscriptMessage,
)


class AgentEngine(AgentInboundPort):
    """只编排用户回合；会话、上下文、模型和工具均通过核心 ports 接入。"""

    def __init__(
        self,
        *,
        model_turn: ModelTurnPort,
        tool_round: ToolRoundPort,
        conversation: ConversationState,
        context: ContextPort,
        compactor: CompactorPort,
        max_turns: int = 12,
    ) -> None:
        if max_turns < 1:
            raise ValueError("max_turns must be positive")
        self._model_turn = model_turn
        self._tool_round = tool_round
        self._conversation = conversation
        self._context = context
        self._compactor = compactor
        self.max_turns = max_turns
        self._tool_round.bind_session(self._conversation.session_id)

    @property
    def session_id(self) -> str:
        """当前 Agent 与之绑定的 session。"""

        return self._conversation.session_id

    @property
    def working_messages(self) -> tuple[TranscriptMessage, ...]:
        """当前模型工作集的只读快照。"""

        return self._conversation.working_messages

    @property
    def message_count(self) -> int:
        return self._conversation.message_count

    @property
    def content_replacement_count(self) -> int:
        return self._conversation.content_replacement_count

    @property
    def compact_count(self) -> int:
        return self._conversation.compact_count

    async def submit(self, prompt: str) -> AgentTurnResult:
        """消费可观察循环并返回终态值。"""

        completed: AgentTurnResult | None = None
        async for event in self.stream(prompt):
            if isinstance(event, AgentTurnCompleted):
                completed = event.result
        if completed is None:
            raise RuntimeError("Agent stream ended without a completed turn")
        return completed

    def stream(self, prompt: str) -> AsyncIterator[AgentEvent]:
        """运行一个用户回合，同时暴露文本和工具生命周期事件。"""

        return self._stream(prompt)

    def submit_stream(self, prompt: str) -> AsyncIterator[AgentEvent]:
        """兼容旧调用名；新的 inbound port 使用 ``stream``。"""

        return self.stream(prompt)

    async def _stream(self, prompt: str) -> AsyncIterator[AgentEvent]:
        if not prompt.strip():
            raise ValueError("Prompt must not be empty")

        user_message = TranscriptMessage(
            role="user",
            origin="human",
            content=(TextBlock(prompt),),
            parent_uuid=self._last_uuid,
        )
        # 首次请求前先写入 Transcript，保证崩溃或网络失败后仍可恢复输入。
        self._conversation.append(user_message)

        input_tokens = 0
        output_tokens = 0
        for turn in range(1, self.max_turns + 1):
            request = await self._plan_with_proactive_compact()
            reactive_attempted = False
            while True:
                response: ModelResponse | None = None
                streamed_text = False
                try:
                    async for model_event in self._model_turn.stream(request):
                        if isinstance(model_event, ModelTextDelta):
                            streamed_text = True
                            yield AgentTextDelta(model_event.text)
                        elif isinstance(model_event, ModelResponseCompleted):
                            if response is not None:
                                raise RuntimeError(
                                    "Provider stream returned multiple final responses"
                                )
                            response = model_event.response
                except ModelContextOverflow:
                    if reactive_attempted:
                        raise
                    reactive_attempted = True
                    await self.compact("reactive")
                    request = await self._plan_request()
                    continue
                break

            if response is None:
                raise RuntimeError("Provider stream ended without a final response")
            if not streamed_text:
                for block in response.content:
                    if isinstance(block, TextBlock):
                        yield AgentTextDelta(block.text)

            input_tokens += response.usage.total_input_tokens
            output_tokens += response.usage.output_tokens
            assistant_message = TranscriptMessage(
                role="assistant",
                origin="model",
                content=response.content,
                parent_uuid=self._last_uuid,
                usage=response.usage,
            )
            # 先持久化 assistant 的完整 tool_use，再进入执行阶段。
            self._conversation.append(assistant_message)

            final_text = "\n".join(
                block.text for block in response.content if isinstance(block, TextBlock)
            ).strip()
            tool_calls = tuple(
                block for block in response.content if isinstance(block, ToolUseBlock)
            )
            if not tool_calls:
                yield AgentTurnCompleted(
                    AgentTurnResult(
                        text=final_text,
                        turns=turn,
                        usage=TokenUsage(input_tokens, output_tokens),
                    )
                )
                return

            result_message: TranscriptMessage | None = None
            results: list[ToolResultBlock] = []
            round_cancelled = False
            try:
                async for tool_event in self._tool_round.run_round(
                    tool_calls, assistant_message
                ):
                    if isinstance(tool_event, ToolCallStarted):
                        yield AgentToolStarted(
                            tool_event.call.id,
                            tool_event.call.name,
                            tool_event.call.input,
                            tool_event.presentation,
                        )
                    elif isinstance(tool_event, ToolCallFinished):
                        results.append(tool_event.result)
                        yield AgentToolFinished(
                            tool_event.call.id,
                            tool_event.call.name,
                            tool_event.result.is_error,
                            tool_event.presentation,
                        )
                    elif isinstance(tool_event, ToolRoundCompleted):
                        result_message = tool_event.message
                        results = list(tool_event.results)
                        round_cancelled = tool_event.cancelled

                if result_message is None:
                    if not results:
                        raise RuntimeError("Tool round ended without results")
                    self._conversation.append_tool_results(results, assistant_message)
                else:
                    self._conversation.append(result_message)
            except asyncio.CancelledError:
                # 自定义 ToolRoundPort 也必须满足协议闭合；适配器通常
                # 已经发出了所有取消结果，这里只负责兜底并持久化一次。
                if result_message is None:
                    results = _cancelled_results(
                        tool_calls,
                        self._tool_round,
                        results,
                    )
                    self._conversation.append_tool_results(results, assistant_message)
                else:
                    self._conversation.append(result_message)
                raise
            if round_cancelled:
                raise asyncio.CancelledError

        raise RuntimeError(
            f"Agent reached max_turns={self.max_turns} after the last tool result"
        )

    def state(self) -> AgentState:
        """返回当前 session 的只读状态。"""

        return AgentState(
            session_id=self._conversation.session_id,
            message_count=self._conversation.message_count,
            history_message_count=self._conversation.history_message_count,
            content_replacement_count=self._conversation.content_replacement_count,
            compact_count=self._conversation.compact_count,
        )

    def context_state(self) -> AgentContextState:
        """返回当前工作集的预算报告。"""

        return AgentContextState(
            budget=self._context.inspect(self._conversation.context_snapshot()),
            working_message_count=self._conversation.message_count,
            replacement_count=self._conversation.content_replacement_count,
            compact_count=self._conversation.compact_count,
        )

    async def compact(self, trigger: CompactTrigger = "manual") -> CompactBoundary:
        """生成并原子提交一次摘要边界。"""

        outcome = await self._compactor.compact(
            self._conversation.context_snapshot(), trigger
        )
        return self._conversation.commit_compaction(outcome)

    def resume(self, repository: SessionRepository) -> AgentSessionView:
        """校验并恢复另一会话，失败时保持当前状态。"""

        messages = self._conversation.resume(repository)
        self._tool_round.bind_session(repository.session_id)
        return AgentSessionView(
            state=self.state(),
            history=self._project_history(messages),
        )

    def _project_history(
        self, messages: tuple[TranscriptMessage, ...]
    ) -> tuple[
        AgentHistoryUserMessage
        | AgentHistoryAssistantMessage
        | AgentHistorySystemMessage
        | AgentHistoryToolCall,
        ...,
    ]:
        results = {
            block.tool_use_id: block
            for message in messages
            for block in message.content
            if isinstance(block, ToolResultBlock)
        }
        history: list[
            AgentHistoryUserMessage
            | AgentHistoryAssistantMessage
            | AgentHistorySystemMessage
            | AgentHistoryToolCall
        ] = []
        for message in messages:
            if message.origin == "human":
                text = "\n".join(
                    block.text
                    for block in message.content
                    if isinstance(block, TextBlock)
                )
                if text:
                    history.append(AgentHistoryUserMessage(text))
                continue
            if message.origin == "system":
                history.append(AgentHistorySystemMessage("Conversation compacted"))
                continue
            if message.origin != "model":
                continue
            for block in message.content:
                if isinstance(block, TextBlock):
                    if block.text:
                        history.append(AgentHistoryAssistantMessage(block.text))
                elif isinstance(block, ToolUseBlock):
                    result = results.get(block.id)
                    history.append(
                        AgentHistoryToolCall(
                            tool_use_id=block.id,
                            use=self._tool_round.present_use(block),
                            result=self._tool_round.present_stored_result(
                                block, result
                            ),
                            is_error=result is None or result.is_error,
                        )
                    )
        return tuple(history)

    async def _plan_with_proactive_compact(self) -> ContextPlan:
        try:
            return await self._plan_request()
        except ContextOverflow:
            await self.compact("auto")
            return await self._plan_request()

    async def _plan_request(self) -> ContextPlan:
        request = self._context.plan(self._conversation.context_snapshot())
        for replacement in request.new_content_replacements:
            self._conversation.append_content_replacement(replacement)
        return request

    @property
    def _last_uuid(self) -> str | None:
        return (
            self._conversation.working_messages[-1].uuid
            if self._conversation.working_messages
            else None
        )


def _cancelled_results(
    calls: tuple[ToolUseBlock, ...],
    tool_round: ToolRoundPort,
    existing: list[ToolResultBlock],
) -> list[ToolResultBlock]:
    message = "Tool execution was cancelled."
    by_id = {result.tool_use_id: result for result in existing}
    results: list[ToolResultBlock] = []
    for call in calls:
        if call.id in by_id:
            results.append(by_id[call.id])
            continue
        result = ToolResultBlock(
            tool_use_id=call.id,
            content=message,
            is_error=True,
        )
        presentation = tool_round.present_stored_result(call, result)
        results.append(
            ToolResultBlock(
                tool_use_id=result.tool_use_id,
                content=result.content,
                is_error=True,
                presentation=presentation,
            )
        )
    return results
