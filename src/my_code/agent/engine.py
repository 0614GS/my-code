"""单个持久化用户 Turn 中的模型 → 工具 → 模型状态机。"""

import asyncio
from collections.abc import AsyncIterator

from my_code.agent.events import (
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
from my_code.agent.models import (
    AgentMaxStepsReached,
    AgentTurnInput,
    AgentTurnOutcome,
    AgentTurnSucceeded,
)
from my_code.context.engine import ContextEngine
from my_code.context.models import ContextOverflow, ContextPlan
from my_code.context.session import ContextRuntime
from my_code.conversation.attachments import (
    AttachmentPayload,
    ToolDiscoveryInvalidationAttachment,
    ToolSearchListingAttachment,
)
from my_code.conversation.models import (
    AssistantMessage,
    HumanMessage,
    ReasoningContent,
    TextContent,
    ToolCall,
    ToolResult,
    ToolResultBatch,
)
from my_code.conversation.state import CompactTrigger
from my_code.model.client import ModelClient
from my_code.model.errors import ModelContextOverflow
from my_code.model.events import (
    ModelOutputCompleted,
    ModelReasoningCompleted,
    ModelReasoningDelta,
    ModelReasoningStarted,
    ModelTextCompleted,
    ModelTextDelta,
    ModelTextStarted,
)
from my_code.model.primitives import (
    ProviderReplayRecord,
    TokenUsage,
    replay_content_id,
)
from my_code.model.request import ModelOutput, ModelTextBlock, ModelToolUseBlock
from my_code.model.tool_search import ToolSearchMode
from my_code.permissions.models import PermissionUpdate
from my_code.sessions.session import Session
from my_code.tools.base import ToolExposure
from my_code.tools.catalog import ToolCatalog, ToolCatalogSnapshot
from my_code.tools.discovery import (
    TOOL_SEARCH_NAME,
    ToolExposureSnapshot,
    restored_discoveries,
)
from my_code.tools.round_executor import (
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
        context: ContextEngine,
        tool_catalog: ToolCatalog,
        tool_search_mode: ToolSearchMode | None = None,
        max_steps: int | None = None,
    ) -> None:
        if max_steps is not None and max_steps < 1:
            raise ValueError("max_steps must be positive")
        self._model_call = model_call
        self._tool_round = tool_round
        self._context = context
        self._tool_catalog = tool_catalog
        self._tool_search_mode = tool_search_mode
        self.max_steps = max_steps

    async def submit(
        self,
        session: Session,
        runtime: ContextRuntime,
        turn_input: AgentTurnInput,
    ) -> AgentTurnOutcome:
        """消费可观察循环并返回终态值。"""

        completed: AgentTurnOutcome | None = None
        async for event in self.stream(session, runtime, turn_input):
            if isinstance(event, (AgentTurnSucceeded, AgentMaxStepsReached)):
                completed = event
        if completed is None:
            raise RuntimeError("Agent stream ended without a completed turn")
        return completed

    def stream(
        self,
        session: Session,
        runtime: ContextRuntime,
        turn_input: AgentTurnInput,
    ) -> AsyncIterator[AgentEvent]:
        """运行一个用户回合，同时暴露文本和工具生命周期事件。"""

        return self._stream(session, runtime, turn_input)

    def stream_continuation(
        self,
        session: Session,
        runtime: ContextRuntime,
    ) -> AsyncIterator[AgentEvent]:
        """Continue from canonical conversation facts without a human message."""

        return self._stream(session, runtime, None)

    async def _stream(
        self,
        session: Session,
        runtime: ContextRuntime,
        turn_input: AgentTurnInput | None,
    ) -> AsyncIterator[AgentEvent]:
        if turn_input is not None:
            user_message = HumanMessage(
                content=turn_input.prompt,
                parent_uuid=_last_uuid(session),
            )
            # 首次请求前先写入 Transcript，保证崩溃或网络失败后仍可恢复输入。
            session.append_human_message(user_message)
            for attachment in turn_input.attachments:
                session.append_attachment(attachment)

        input_tokens = 0
        output_tokens = 0
        step_count = 0
        while True:
            step_count += 1
            catalog = self._tool_catalog.snapshot()
            if self._tool_search_mode is None:
                tools: ToolCatalogSnapshot | ToolExposureSnapshot = catalog
            else:
                discoveries = restored_discoveries(session.conversation)
                tools = ToolExposureSnapshot.build(
                    catalog, self._tool_search_mode, discoveries
                )
                invalidated = tools.invalidated(discoveries)
                if invalidated:
                    session.append_attachment(
                        ToolDiscoveryInvalidationAttachment(invalidated)
                    )
                    discoveries = restored_discoveries(session.conversation)
                    tools = ToolExposureSnapshot.build(
                        catalog, self._tool_search_mode, discoveries
                    )
                searchable_names = tuple(
                    sorted(
                        tool.definition.name
                        for tool in catalog.tools
                        if tool.exposure is ToolExposure.SEARCHABLE
                    )
                )
                if (
                    catalog.get(TOOL_SEARCH_NAME) is not None
                    and _latest_search_listing(session) != searchable_names
                ):
                    session.append_attachment(
                        ToolSearchListingAttachment(searchable_names)
                    )
            request = await self._plan_with_proactive_compact(session, runtime, tools)
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
                    await self._compact_for_retry(session, runtime, "reactive")
                    request = await self._plan_request(session, runtime, tools)
                    continue
                break

            if response is None:
                raise RuntimeError("Provider stream ended without a final response")

            input_tokens += response.usage.total_input_tokens
            output_tokens += response.usage.output_tokens
            assistant_message = AssistantMessage(
                content=tuple(
                    TextContent(block.text)
                    if isinstance(block, ModelTextBlock)
                    else ToolCall(block.id, block.name, block.input)
                    if isinstance(block, ModelToolUseBlock)
                    else ReasoningContent(block.id, block.presentation)
                    for block in response.content
                ),
                parent_uuid=_last_uuid(session),
                usage=response.usage,
                provider_binding=request.request_binding,
                request_input_tokens_estimate=request.request_input_tokens_estimate,
            )
            replay_records = tuple(
                ProviderReplayRecord(
                    assistant_message.uuid,
                    replay_content_id(index),
                    block.continuation,
                )
                for index, block in enumerate(response.content)
                if block.continuation is not None
            )
            # 先持久化 assistant 的完整 tool_use，再进入执行阶段。
            session.append_assistant_message(
                assistant_message, replay_records=replay_records
            )
            final_text = "\n".join(
                block.text
                for block in response.content
                if isinstance(block, ModelTextBlock)
            ).strip()
            tool_calls = tuple(
                block
                for block in assistant_message.content
                if isinstance(block, ToolCall)
            )
            if not tool_calls:
                yield AgentTurnSucceeded(
                    text=final_text,
                    completed_steps=step_count,
                    usage=TokenUsage(input_tokens, output_tokens),
                )
                return

            result_message: ToolResultBatch | None = None
            results: list[ToolResult] = []
            round_cancelled = False
            round_attachments: tuple[AttachmentPayload, ...] = ()
            round_permission_updates: tuple[PermissionUpdate, ...] = ()
            try:
                async for tool_event in self._tool_round.run_round(
                    tool_calls,
                    assistant_message,
                    tools=tools,
                    run_id=session.session_id,
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
                        round_attachments = tool_event.new_attachments
                        round_permission_updates = tool_event.permission_updates

                if result_message is None:
                    if not results:
                        raise RuntimeError("Tool round ended without results")
                    result_message = ToolResultBatch(
                        tuple(results),
                        assistant_message.uuid,
                        parent_uuid=assistant_message.uuid,
                    )
                session.commit_tool_round(
                    result_message,
                    round_attachments,
                )
                if round_permission_updates:
                    self._tool_round.apply_permission_updates(round_permission_updates)
                yield AgentConversationUpdated()
            except asyncio.CancelledError:
                # ToolRoundExecutor 通常已经发出所有取消结果；Agent 只保留
                # 最终协议兜底并持久化一次。
                if result_message is None:
                    results = _cancelled_results(
                        tool_calls, results, self._tool_round, tools
                    )
                    result_message = ToolResultBatch(
                        tuple(results),
                        assistant_message.uuid,
                        parent_uuid=assistant_message.uuid,
                    )
                session.commit_tool_round(
                    result_message,
                    round_attachments,
                )
                if round_permission_updates:
                    self._tool_round.apply_permission_updates(round_permission_updates)
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

    async def _compact_for_retry(
        self,
        session: Session,
        runtime: ContextRuntime,
        trigger: CompactTrigger,
    ) -> None:
        """在当前 turn 内生成并提交 auto/reactive compact。"""

        outcome = await self._context.compact(session.context_planning_state(), trigger)
        session.commit_compaction(
            outcome.replacements, outcome.summary, outcome.boundary
        )

    async def _plan_with_proactive_compact(
        self,
        session: Session,
        runtime: ContextRuntime,
        tools: ToolCatalogSnapshot | ToolExposureSnapshot,
    ) -> ContextPlan:
        try:
            return await self._plan_request(session, runtime, tools)
        except ContextOverflow:
            await self._compact_for_retry(session, runtime, "auto")
            return await self._plan_request(session, runtime, tools)

    async def _plan_request(
        self,
        session: Session,
        runtime: ContextRuntime,
        tools: ToolCatalogSnapshot | ToolExposureSnapshot,
    ) -> ContextPlan:
        derived = self._context.derive_attachments(
            session.attachment_derivation_state()
        )
        for attachment in derived:
            session.append_attachment(attachment)
            self._context.acknowledge_attachments((attachment,))
        request = self._context.plan(
            session.context_planning_state(),
            runtime,
            tools=tools.definitions,
        )
        for replacement in request.new_content_replacements:
            session.commit_content_replacement(replacement)
        return request


def _cancelled_results(
    calls: tuple[ToolCall, ...],
    existing: list[ToolResult],
    tool_round: ToolRoundExecutor,
    tools: ToolCatalogSnapshot | ToolExposureSnapshot,
) -> list[ToolResult]:
    by_id = {result.tool_use_id: result for result in existing}
    results: list[ToolResult] = []
    for call in calls:
        if call.id in by_id:
            results.append(by_id[call.id])
            continue
        result = tool_round.executor.cancelled_result(call, tools=tools)
        results.append(result)
    return results


def _last_uuid(session: Session) -> str | None:
    return session.causal_head_uuid


def _latest_search_listing(session: Session) -> tuple[str, ...] | None:
    from my_code.conversation.models import AttachmentMessage

    for entry in reversed(session.context_entries):
        if isinstance(entry, AttachmentMessage) and isinstance(
            entry.payload, ToolSearchListingAttachment
        ):
            return entry.payload.names
    return None


__all__ = [
    "AgentEngine",
]
