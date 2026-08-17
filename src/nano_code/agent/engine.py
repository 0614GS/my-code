"""单个持久化用户 Turn 中的模型 → 工具 → 模型状态机。"""

import asyncio
from collections.abc import AsyncIterator

from nano_code.agent.contracts.context import ContextPlan
from nano_code.agent.contracts.inbound import (
    AgentContextStatus,
    AgentHistoryAssistantMessage,
    AgentHistorySystemMessage,
    AgentHistoryToolCall,
    AgentHistoryUserMessage,
    AgentMaxStepsReached,
    AgentSessionView,
    AgentStatus,
    AgentTurnInput,
    AgentTurnOutcome,
    AgentTurnSucceeded,
)
from nano_code.agent.contracts.model import (
    ModelOutput,
    ModelOutputCompleted,
    ModelTextBlock,
    ModelTextDelta,
    ModelToolUseBlock,
)
from nano_code.agent.contracts.session import (
    AttachmentDelivery,
    CompactBoundary,
    CompactTrigger,
)
from nano_code.agent.contracts.tool import (
    ToolCallFinished,
    ToolCallStarted,
    ToolRoundCompleted,
)
from nano_code.agent.conversation import ConversationState
from nano_code.agent.errors import ContextOverflow, ModelContextOverflow
from nano_code.agent.events import (
    AgentEvent,
    AgentStepLimitReached,
    AgentTextDelta,
    AgentTodoListUpdated,
    AgentToolFinished,
    AgentToolStarted,
    AgentTurnCompleted,
)
from nano_code.agent.ports.compaction import CompactorPort
from nano_code.agent.ports.context import ContextPort
from nano_code.agent.ports.inbound import AgentInboundPort
from nano_code.agent.ports.model import ModelCallPort
from nano_code.agent.ports.session import SessionRepository
from nano_code.agent.ports.tool import ToolRoundPort
from nano_code.messages import (
    AssistantMessage,
    ConversationMessage,
    ConversationSummaryMessage,
    HumanMessage,
    TextContent,
    TokenUsage,
    ToolCall,
    ToolResult,
    ToolResultsMessage,
)
from nano_code.todos.models import TodoItem
from nano_code.todos.projection import project_todos


class AgentEngine(AgentInboundPort):
    """只编排用户回合；会话、上下文、模型和工具均通过核心 ports 接入。"""

    def __init__(
        self,
        *,
        model_call: ModelCallPort,
        tool_round: ToolRoundPort,
        conversation: ConversationState,
        context: ContextPort,
        compactor: CompactorPort,
        max_steps: int | None = None,
    ) -> None:
        if max_steps is not None and max_steps < 1:
            raise ValueError("max_steps must be positive")
        self._model_call = model_call
        self._tool_round = tool_round
        self._conversation = conversation
        self._context = context
        self._compactor = compactor
        self.max_steps = max_steps
        self._tool_round.bind_session(self._conversation.session_id)

    async def submit(self, turn_input: AgentTurnInput) -> AgentTurnOutcome:
        """消费可观察循环并返回终态值。"""

        completed: AgentTurnOutcome | None = None
        async for event in self.stream(turn_input):
            if isinstance(event, AgentTurnCompleted):
                completed = event.result
            elif isinstance(event, AgentStepLimitReached):
                completed = event.result
        if completed is None:
            raise RuntimeError("Agent stream ended without a completed turn")
        return completed

    def stream(self, turn_input: AgentTurnInput) -> AsyncIterator[AgentEvent]:
        """运行一个用户回合，同时暴露文本和工具生命周期事件。"""

        return self._stream(turn_input)

    async def _stream(self, turn_input: AgentTurnInput) -> AsyncIterator[AgentEvent]:
        user_message = HumanMessage(
            content=turn_input.prompt,
            parent_uuid=self._last_uuid,
        )
        # 首次请求前先写入 Transcript，保证崩溃或网络失败后仍可恢复输入。
        self._conversation.append(user_message)
        self._conversation.add_attachment_deliveries(
            tuple(
                AttachmentDelivery(user_message.uuid, attachment)
                for attachment in turn_input.attachments
            )
        )

        input_tokens = 0
        output_tokens = 0
        step_count = 0
        while True:
            step_count += 1
            request = await self._plan_with_proactive_compact()
            reactive_attempted = False
            while True:
                response: ModelOutput | None = None
                streamed_text = False
                try:
                    async for model_event in self._model_call.stream(request.request):
                        if isinstance(model_event, ModelTextDelta):
                            streamed_text = True
                            yield AgentTextDelta(model_event.text)
                        elif isinstance(model_event, ModelOutputCompleted):
                            if response is not None:
                                raise RuntimeError(
                                    "Provider stream returned multiple final responses"
                                )
                            response = model_event.output
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
                    if isinstance(block, ModelTextBlock):
                        yield AgentTextDelta(block.text)

            input_tokens += response.usage.total_input_tokens
            output_tokens += response.usage.output_tokens
            assistant_message = AssistantMessage(
                content=tuple(
                    TextContent(block.text)
                    if isinstance(block, ModelTextBlock)
                    else ToolCall(block.id, block.name, block.input)
                    for block in response.content
                ),
                parent_uuid=self._last_uuid,
                usage=response.usage,
            )
            # 先持久化 assistant 的完整 tool_use，再进入执行阶段。
            self._conversation.append(assistant_message)

            final_text = "\n".join(
                block.text
                for block in response.content
                if isinstance(block, ModelTextBlock)
            ).strip()
            tool_calls = tuple(
                ToolCall(block.id, block.name, block.input)
                for block in response.content
                if isinstance(block, ModelToolUseBlock)
            )
            if not tool_calls:
                yield AgentTurnCompleted(
                    AgentTurnSucceeded(
                        text=final_text,
                        completed_steps=step_count,
                        usage=TokenUsage(input_tokens, output_tokens),
                    )
                )
                return

            result_message: ToolResultsMessage | None = None
            results: list[ToolResult] = []
            round_cancelled = False
            todos_before = self._todos
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
                        results = list(tool_event.message.content)
                        round_cancelled = tool_event.cancelled

                if result_message is None:
                    if not results:
                        raise RuntimeError("Tool round ended without results")
                    self._conversation.append_tool_results(results, assistant_message)
                else:
                    self._conversation.append(result_message)
                todos_after = self._todos
                if todos_after != todos_before:
                    yield AgentTodoListUpdated(todos_after)
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
                todos_after = self._todos
                if todos_after != todos_before:
                    yield AgentTodoListUpdated(todos_after)
                raise
            if round_cancelled:
                raise asyncio.CancelledError
            if self.max_steps is not None and step_count >= self.max_steps:
                yield AgentStepLimitReached(
                    AgentMaxStepsReached(
                        max_steps=self.max_steps,
                        completed_steps=step_count,
                        usage=TokenUsage(input_tokens, output_tokens),
                    )
                )
                return

    def status(self) -> AgentStatus:
        """返回当前 session 的只读状态。"""

        return AgentStatus(
            session_id=self._conversation.session_id,
            working_message_count=self._conversation.message_count,
            history_message_count=self._conversation.history_message_count,
            content_replacement_count=self._conversation.content_replacement_count,
            compact_count=self._conversation.compact_count,
            todos=self._todos,
        )

    def context_status(self) -> AgentContextStatus:
        """返回当前工作集的预算报告。"""

        return AgentContextStatus(
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
            status=self.status(),
            history=self._project_history(messages),
        )

    def _project_history(
        self, messages: tuple[ConversationMessage, ...]
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
            if isinstance(message, ToolResultsMessage)
            for block in message.content
            if isinstance(block, ToolResult)
        }
        history: list[
            AgentHistoryUserMessage
            | AgentHistoryAssistantMessage
            | AgentHistorySystemMessage
            | AgentHistoryToolCall
        ] = []
        for message in messages:
            if isinstance(message, HumanMessage):
                history.append(AgentHistoryUserMessage(message.content))
                continue
            if isinstance(message, ConversationSummaryMessage):
                history.append(AgentHistorySystemMessage("Conversation compacted"))
                continue
            if not isinstance(message, AssistantMessage):
                continue
            for block in message.content:
                if isinstance(block, TextContent):
                    if block.text:
                        history.append(AgentHistoryAssistantMessage(block.text))
                elif isinstance(block, ToolCall):
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
        self._conversation.add_attachment_deliveries(request.new_attachment_deliveries)
        return request

    @property
    def _last_uuid(self) -> str | None:
        return (
            self._conversation.working_messages[-1].uuid
            if self._conversation.working_messages
            else None
        )

    @property
    def _todos(self) -> tuple[TodoItem, ...]:
        return project_todos(self._conversation.history).todos


def _cancelled_results(
    calls: tuple[ToolCall, ...],
    tool_round: ToolRoundPort,
    existing: list[ToolResult],
) -> list[ToolResult]:
    message = "Tool execution was cancelled."
    by_id = {result.tool_use_id: result for result in existing}
    results: list[ToolResult] = []
    for call in calls:
        if call.id in by_id:
            results.append(by_id[call.id])
            continue
        result = ToolResult(
            tool_use_id=call.id,
            content=message,
            is_error=True,
        )
        presentation = tool_round.present_stored_result(call, result)
        results.append(
            ToolResult(
                tool_use_id=result.tool_use_id,
                content=result.content,
                is_error=True,
                presentation=presentation,
            )
        )
    return results
