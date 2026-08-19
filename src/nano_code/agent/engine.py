"""单个持久化用户 Turn 中的模型 → 工具 → 模型状态机。"""

import asyncio
from collections.abc import AsyncIterator

from nano_code.agent.events import (
    AgentConversationUpdated,
    AgentEvent,
    AgentReasoningCompleted,
    AgentReasoningDelta,
    AgentReasoningStarted,
    AgentTextCompleted,
    AgentTextDelta,
    AgentTextStarted,
    AgentToolFinished,
    AgentToolStarted,
)
from nano_code.agent.models import (
    AgentMaxStepsReached,
    AgentTurnInput,
    AgentTurnOutcome,
    AgentTurnSucceeded,
)
from nano_code.context.compaction import CompactionCoordinator
from nano_code.context.models import ContextBudget, ContextOverflow, ContextPlan
from nano_code.context.planner import ContextBuilder
from nano_code.context.session import (
    AttachmentDelivery,
    ContextSession,
    ContextSnapshot,
)
from nano_code.conversation.models import (
    AssistantMessage,
    HumanMessage,
    ReasoningContent,
    TextContent,
    ToolCall,
    ToolResult,
    ToolResultsMessage,
)
from nano_code.conversation.state import CompactBoundary, CompactTrigger
from nano_code.model.client import ModelClient
from nano_code.model.errors import ModelContextOverflow
from nano_code.model.events import (
    ModelOutputCompleted,
    ModelReasoningCompleted,
    ModelReasoningDelta,
    ModelReasoningStarted,
    ModelTextCompleted,
    ModelTextDelta,
    ModelTextStarted,
)
from nano_code.model.primitives import TokenUsage
from nano_code.model.request import ModelOutput, ModelTextBlock, ModelToolUseBlock
from nano_code.sessions.session import Session
from nano_code.tools.presentation import ToolResultPresentation, ToolUsePresentation
from nano_code.tools.result_store import ToolResultStore
from nano_code.tools.round_executor import (
    ToolCallFinished,
    ToolCallStarted,
    ToolRoundCompleted,
    ToolRoundExecutor,
)


class AgentEngine:
    """只编排一次 turn；活动会话及其临时上下文由调用方传入。"""

    def __init__(
        self,
        *,
        model_call: ModelClient,
        tool_round: ToolRoundExecutor,
        context: ContextBuilder,
        compactor: CompactionCoordinator,
        max_steps: int | None = None,
    ) -> None:
        if max_steps is not None and max_steps < 1:
            raise ValueError("max_steps must be positive")
        self._model_call = model_call
        self._tool_round = tool_round
        self._context = context
        self._compactor = compactor
        self.max_steps = max_steps

    async def submit(
        self,
        session: Session,
        context_session: ContextSession,
        result_store: ToolResultStore,
        turn_input: AgentTurnInput,
    ) -> AgentTurnOutcome:
        """消费可观察循环并返回终态值。"""

        completed: AgentTurnOutcome | None = None
        async for event in self.stream(
            session, context_session, result_store, turn_input
        ):
            if isinstance(event, (AgentTurnSucceeded, AgentMaxStepsReached)):
                completed = event
        if completed is None:
            raise RuntimeError("Agent stream ended without a completed turn")
        return completed

    def stream(
        self,
        session: Session,
        context_session: ContextSession,
        result_store: ToolResultStore,
        turn_input: AgentTurnInput,
    ) -> AsyncIterator[AgentEvent]:
        """运行一个用户回合，同时暴露文本和工具生命周期事件。"""

        return self._stream(session, context_session, result_store, turn_input)

    async def _stream(
        self,
        session: Session,
        context_session: ContextSession,
        result_store: ToolResultStore,
        turn_input: AgentTurnInput,
    ) -> AsyncIterator[AgentEvent]:
        user_message = HumanMessage(
            content=turn_input.prompt,
            parent_uuid=_last_uuid(session),
        )
        # 首次请求前先写入 Transcript，保证崩溃或网络失败后仍可恢复输入。
        session.append(user_message)
        context_session.add(
            tuple(
                AttachmentDelivery(user_message.uuid, attachment)
                for attachment in turn_input.attachments
            ),
            session.conversation.snapshot(),
        )

        input_tokens = 0
        output_tokens = 0
        step_count = 0
        while True:
            step_count += 1
            request = await self._plan_with_proactive_compact(session, context_session)
            reactive_attempted = False
            while True:
                response: ModelOutput | None = None
                expected_sequence = 0
                active_display: str | None = None
                active_disclosure: str | None = None
                try:
                    async for model_event in self._model_call.stream(request.request):
                        if model_event.sequence_number != expected_sequence:
                            raise RuntimeError(
                                "Provider stream returned a non-contiguous sequence"
                            )
                        expected_sequence += 1
                        payload = model_event.payload
                        if isinstance(payload, ModelTextStarted):
                            if active_display is not None:
                                raise RuntimeError(
                                    "Provider stream started overlapping display blocks"
                                )
                            active_display = "text"
                            active_disclosure = None
                            yield AgentTextStarted()
                        elif isinstance(payload, ModelTextDelta):
                            if active_display != "text":
                                raise RuntimeError(
                                    "Provider stream returned text outside a text block"
                                )
                            yield AgentTextDelta(payload.text)
                        elif isinstance(payload, ModelTextCompleted):
                            if active_display != "text":
                                raise RuntimeError(
                                    "Provider stream completed an inactive text block"
                                )
                            yield AgentTextCompleted(payload.text)
                            active_display = None
                        elif isinstance(payload, ModelReasoningStarted):
                            if active_display is not None:
                                raise RuntimeError(
                                    "Provider stream started overlapping display blocks"
                                )
                            active_display = "reasoning"
                            active_disclosure = payload.disclosure
                            yield AgentReasoningStarted(payload.disclosure)
                        elif isinstance(payload, ModelReasoningDelta):
                            if active_display != "reasoning":
                                raise RuntimeError(
                                    "Provider stream returned reasoning outside a block"
                                )
                            if payload.disclosure != active_disclosure:
                                raise RuntimeError(
                                    "Provider stream changed reasoning disclosure "
                                    "mid-block"
                                )
                            yield AgentReasoningDelta(
                                payload.disclosure,
                                payload.part_index,
                                payload.text,
                            )
                        elif isinstance(payload, ModelReasoningCompleted):
                            if active_display != "reasoning":
                                raise RuntimeError(
                                    "Provider stream completed inactive reasoning"
                                )
                            yield AgentReasoningCompleted(payload.presentation)
                            active_display = None
                            active_disclosure = None
                        elif isinstance(payload, ModelOutputCompleted):
                            if active_display is not None:
                                raise RuntimeError(
                                    "Provider stream ended with an active display block"
                                )
                            if response is not None:
                                raise RuntimeError(
                                    "Provider stream returned multiple final responses"
                                )
                            response = payload.output
                except ModelContextOverflow:
                    if reactive_attempted:
                        raise
                    reactive_attempted = True
                    await self.compact(session, context_session, "reactive")
                    request = await self._plan_request(session, context_session)
                    continue
                break

            if response is None:
                raise RuntimeError("Provider stream ended without a final response")

            input_tokens += response.usage.total_input_tokens
            output_tokens += response.usage.output_tokens
            assistant_message = AssistantMessage(
                content=tuple(
                    TextContent(block.text, block.continuation)
                    if isinstance(block, ModelTextBlock)
                    else ToolCall(block.id, block.name, block.input, block.continuation)
                    if isinstance(block, ModelToolUseBlock)
                    else ReasoningContent(
                        block.id, block.presentation, block.continuation
                    )
                    for block in response.content
                ),
                parent_uuid=_last_uuid(session),
                usage=response.usage,
                provider_binding=request.request_binding,
                request_input_tokens_estimate=request.request_input_tokens_estimate,
            )
            # 先持久化 assistant 的完整 tool_use，再进入执行阶段。
            session.append(assistant_message)

            final_text = "\n".join(
                block.text
                for block in response.content
                if isinstance(block, ModelTextBlock)
            ).strip()
            tool_calls = tuple(
                ToolCall(block.id, block.name, block.input, block.continuation)
                for block in response.content
                if isinstance(block, ModelToolUseBlock)
            )
            if not tool_calls:
                yield AgentTurnSucceeded(
                    text=final_text,
                    completed_steps=step_count,
                    usage=TokenUsage(input_tokens, output_tokens),
                )
                return

            result_message: ToolResultsMessage | None = None
            results: list[ToolResult] = []
            tool_presentations: dict[str, ToolResultPresentation] = {}
            round_cancelled = False
            try:
                async for tool_event in self._tool_round.run_round(
                    tool_calls,
                    assistant_message,
                    result_store=result_store,
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
                        tool_presentations[tool_event.call.id] = tool_event.presentation
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
                    session.append_tool_results(
                        results,
                        assistant_message,
                        presentations=tool_presentations.items(),
                    )
                else:
                    session.append(
                        result_message, presentations=tool_presentations.items()
                    )
                yield AgentConversationUpdated()
            except asyncio.CancelledError:
                # ToolRoundExecutor 通常已经发出所有取消结果；Agent 只保留
                # 最终协议兜底并持久化一次。
                if result_message is None:
                    results = _cancelled_results(
                        tool_calls,
                        self._tool_round,
                        results,
                    )
                    session.append_tool_results(
                        results,
                        assistant_message,
                        presentations=tool_presentations.items(),
                    )
                else:
                    session.append(
                        result_message, presentations=tool_presentations.items()
                    )
                yield AgentConversationUpdated()
                raise
            if round_cancelled:
                raise asyncio.CancelledError
            if self.max_steps is not None and step_count >= self.max_steps:
                yield AgentMaxStepsReached(
                    max_steps=self.max_steps,
                    completed_steps=step_count,
                    usage=TokenUsage(input_tokens, output_tokens),
                )
                return

    async def compact(
        self,
        session: Session,
        context_session: ContextSession,
        trigger: CompactTrigger = "manual",
    ) -> CompactBoundary:
        """生成并原子提交一次摘要边界。"""

        outcome = await self._compactor.compact(
            _context_snapshot(session, context_session), trigger
        )
        return session.commit_compaction(
            outcome.replacements, outcome.summary, outcome.boundary
        )

    async def _plan_with_proactive_compact(
        self, session: Session, context_session: ContextSession
    ) -> ContextPlan:
        try:
            return await self._plan_request(session, context_session)
        except ContextOverflow:
            await self.compact(session, context_session, "auto")
            return await self._plan_request(session, context_session)

    async def _plan_request(
        self, session: Session, context_session: ContextSession
    ) -> ContextPlan:
        request = self._context.plan(
            _context_snapshot(session, context_session), context_session
        )
        for replacement in request.new_content_replacements:
            session.append_content_replacement(replacement)
        context_session.add(
            request.new_attachment_deliveries,
            session.conversation.snapshot(),
        )
        return request

    def inspect(
        self, session: Session, context_session: ContextSession
    ) -> ContextBudget:
        return self._context.inspect(
            _context_snapshot(session, context_session), context_session
        )

    def present_use(self, call: ToolCall) -> ToolUsePresentation:
        return self._tool_round.present_use(call)

    def present_stored_result(
        self, call: ToolCall, result: ToolResult | None
    ) -> ToolResultPresentation:
        return self._tool_round.present_stored_result(call, result)


def _cancelled_results(
    calls: tuple[ToolCall, ...],
    tool_round: ToolRoundExecutor,
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
        results.append(result)
    return results


def _last_uuid(session: Session) -> str | None:
    return session.working_messages[-1].uuid if session.working_messages else None


def _context_snapshot(
    session: Session, context_session: ContextSession
) -> ContextSnapshot:
    return context_session.snapshot(session.conversation.snapshot())


__all__ = [
    "AgentEngine",
]
