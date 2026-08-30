"""单个持久化用户 Turn 中的模型 → 工具 → 模型状态机。"""

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Literal

from my_code.agent.events import (
    AgentCompactionCompleted,
    AgentCompactionStarted,
    AgentConversationUpdated,
    AgentEvent,
    AgentInputAccepted,
    AgentModelRequestPrepared,
    AgentModelStepCompleted,
    AgentReasoningCompleted,
    AgentReasoningDelta,
    AgentReasoningStarted,
    AgentTextCompleted,
    AgentTextDelta,
    AgentTextStarted,
    AgentToolFinished,
    AgentToolStarted,
    PreparedContextItem,
)
from my_code.agent.models import (
    AgentMaxStepsReached,
    AgentTurnInput,
    AgentTurnOutcome,
    AgentTurnSucceeded,
    PendingInputSource,
    UserTurnInput,
)
from my_code.context.engine import ContextEngine
from my_code.context.models import CompactionOutcome, ContextOverflow, ContextPlan
from my_code.context.session import ContextRuntime
from my_code.conversation.attachments import (
    AttachmentPayload,
    ToolDiscoveryInvalidationAttachment,
    ToolSearchListingAttachment,
)
from my_code.conversation.models import (
    AssistantMessage,
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
    ModelStreamEvent,
    ModelTextCompleted,
    ModelTextDelta,
    ModelTextStarted,
)
from my_code.model.invocation import (
    ModelInputOrigin,
    ModelInputOriginKind,
    ModelInvocation,
    ModelInvocationCoordinator,
    RequestPurpose,
)
from my_code.model.primitives import (
    ProviderReplayRecord,
    ReasoningDisclosure,
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


@dataclass(slots=True)
class _ModelStreamProjector:
    """Validate one provider stream and project its display events."""

    expected_sequence: int = 0
    active_display: Literal["text", "reasoning"] | None = None
    active_disclosure: ReasoningDisclosure | None = None
    response: ModelOutput | None = None

    def project(self, event: ModelStreamEvent) -> AgentEvent | None:
        if event.sequence_number != self.expected_sequence:
            raise RuntimeError("Provider stream returned a non-contiguous sequence")
        self.expected_sequence += 1
        payload = event.payload
        if isinstance(payload, ModelTextStarted):
            if self.active_display is not None:
                raise RuntimeError("Provider stream started overlapping display blocks")
            self.active_display = "text"
            self.active_disclosure = None
            return AgentTextStarted()
        if isinstance(payload, ModelTextDelta):
            if self.active_display != "text":
                raise RuntimeError("Provider stream returned text outside a text block")
            return AgentTextDelta(payload.text)
        if isinstance(payload, ModelTextCompleted):
            if self.active_display != "text":
                raise RuntimeError("Provider stream completed an inactive text block")
            self.active_display = None
            return AgentTextCompleted(payload.text)
        if isinstance(payload, ModelReasoningStarted):
            if self.active_display is not None:
                raise RuntimeError("Provider stream started overlapping display blocks")
            self.active_display = "reasoning"
            self.active_disclosure = payload.disclosure
            return AgentReasoningStarted(payload.disclosure)
        if isinstance(payload, ModelReasoningDelta):
            if self.active_display != "reasoning":
                raise RuntimeError("Provider stream returned reasoning outside a block")
            if payload.disclosure != self.active_disclosure:
                raise RuntimeError(
                    "Provider stream changed reasoning disclosure mid-block"
                )
            return AgentReasoningDelta(
                payload.disclosure,
                payload.part_index,
                payload.text,
            )
        if isinstance(payload, ModelReasoningCompleted):
            if self.active_display != "reasoning":
                raise RuntimeError("Provider stream completed inactive reasoning")
            self.active_display = None
            self.active_disclosure = None
            return AgentReasoningCompleted(payload.presentation)
        if isinstance(payload, ModelOutputCompleted):
            if self.active_display is not None:
                raise RuntimeError("Provider stream ended with an active display block")
            if self.response is not None:
                raise RuntimeError("Provider stream returned multiple final responses")
            self.response = payload.output
        return None

    def completed_output(self) -> ModelOutput:
        if self.response is None:
            raise RuntimeError("Provider stream ended without a final response")
        return self.response


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
        turn_input: AgentTurnInput | Sequence[UserTurnInput],
        pending_source: PendingInputSource | None = None,
    ) -> AgentTurnOutcome:
        """消费可观察循环并返回终态值。"""

        completed: AgentTurnOutcome | None = None
        async for event in self.stream(
            session, runtime, turn_input, pending_source=pending_source
        ):
            if isinstance(event, (AgentTurnSucceeded, AgentMaxStepsReached)):
                completed = event
        if completed is None:
            raise RuntimeError("Agent stream ended without a completed turn")
        return completed

    def stream(
        self,
        session: Session,
        runtime: ContextRuntime,
        turn_input: AgentTurnInput | Sequence[UserTurnInput],
        pending_source: PendingInputSource | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """运行一个用户回合，同时暴露文本和工具生命周期事件。"""

        inputs = (
            (turn_input,)
            if isinstance(turn_input, UserTurnInput)
            else tuple(turn_input)
        )
        return self._stream(session, runtime, inputs, pending_source)

    def stream_continuation(
        self,
        session: Session,
        runtime: ContextRuntime,
        pending_source: PendingInputSource | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Continue from canonical conversation facts without a human message."""

        return self._stream(session, runtime, (), pending_source)

    async def _stream(
        self,
        session: Session,
        runtime: ContextRuntime,
        initial_inputs: tuple[UserTurnInput, ...],
        pending_source: PendingInputSource | None,
    ) -> AsyncIterator[AgentEvent]:
        # The first request is itself a safe boundary.  Direct headless inputs
        # and all host-queued inputs visible at this point form one durable batch.
        accepted = await self._accept_boundary(session, initial_inputs, pending_source)
        for event in accepted:
            yield event

        input_tokens = 0
        output_tokens = 0
        step_count = 0
        continuation = not bool(accepted)
        while True:
            step_count += 1
            tools = self._snapshot_tools(session)
            try:
                request = await self._plan_request(session, runtime, tools)
            except ContextOverflow:
                yield AgentCompactionStarted("auto")
                outcome = await self._compact_for_retry(session, runtime, "auto")
                yield AgentCompactionCompleted("auto", outcome.usage)
                request = await self._plan_request(session, runtime, tools)
            reactive_attempted = False
            while True:
                projector = _ModelStreamProjector()
                try:
                    origins = request.provenance or tuple(
                        ModelInputOrigin(ModelInputOriginKind.CONVERSATION_ENTRY)
                        for _ in request.request.input
                    )
                    invocation = ModelInvocation(
                        request=request.request,
                        origins=origins,
                        purpose=(
                            RequestPurpose.CONTINUATION
                            if continuation
                            else RequestPurpose.AGENT
                        ),
                        causal_head=session.causal_head_uuid,
                        step=step_count,
                        attempt=2 if reactive_attempted else 1,
                        budget=request.budget,
                    )
                    previous_refs = {
                        ref
                        for audited in session.request_audit_snapshot().requests
                        for ref in audited.manifest.input_refs
                    }
                    coordinator = ModelInvocationCoordinator(self._model_call, session)
                    receipt = coordinator.prepare(invocation)
                    injections = _prepared_injections(
                        request,
                        receipt.input_refs,
                        previous_refs,
                        origins=origins,
                    )
                    yield AgentModelRequestPrepared(
                        invocation.request_id,
                        receipt.request_number,
                        invocation.purpose.value,
                        injections,
                    )
                    async for model_event in coordinator.stream(invocation):
                        event = projector.project(model_event)
                        if event is not None:
                            yield event
                except ModelContextOverflow:
                    if reactive_attempted:
                        raise
                    reactive_attempted = True
                    yield AgentCompactionStarted("reactive")
                    outcome = await self._compact_for_retry(
                        session, runtime, "reactive"
                    )
                    yield AgentCompactionCompleted("reactive", outcome.usage)
                    request = await self._plan_request(session, runtime, tools)
                    continue
                break

            response = projector.completed_output()

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
            yield AgentModelStepCompleted(step_count, bool(tool_calls))
            if not tool_calls:
                if self.max_steps is not None and step_count >= self.max_steps:
                    yield AgentTurnSucceeded(
                        text=final_text,
                        completed_steps=step_count,
                        usage=TokenUsage(input_tokens, output_tokens),
                    )
                    return
                accepted = await self._accept_boundary(session, (), pending_source)
                if accepted:
                    continuation = False
                    for event in accepted:
                        yield event
                    # A no-tool response is a complete step, not necessarily the
                    # end of an interactive invocation when steering is waiting.
                    continue
                yield AgentTurnSucceeded(
                    text=final_text,
                    completed_steps=step_count,
                    usage=TokenUsage(input_tokens, output_tokens),
                )
                return

            async for event in self._stream_tool_round(
                session,
                assistant_message,
                tool_calls,
                tools,
            ):
                yield event
            if self.max_steps is not None and step_count >= self.max_steps:
                yield AgentMaxStepsReached(
                    max_steps=self.max_steps,
                    completed_steps=step_count,
                    usage=TokenUsage(input_tokens, output_tokens),
                )
                return
            accepted = await self._accept_boundary(session, (), pending_source)
            if accepted:
                continuation = False
            for event in accepted:
                yield event

    async def _accept_boundary(
        self,
        session: Session,
        initial: tuple[UserTurnInput, ...],
        pending_source: PendingInputSource | None,
    ) -> tuple[AgentInputAccepted, ...]:
        pending = (
            await pending_source.drain_pending() if pending_source is not None else ()
        )
        inputs = (*initial, *pending)
        if not inputs:
            return ()
        session.commit_user_inputs((item.prompt, item.attachments) for item in inputs)
        pending_ids = tuple(
            item.input_id for item in pending if item.input_id is not None
        )
        if pending_source is not None and pending_ids:
            # Queue state changes only after the persistence-first commit.
            pending_source.accept_pending(pending_ids)
        return tuple(AgentInputAccepted(item.input_id, item.prompt) for item in inputs)

    def _snapshot_tools(
        self,
        session: Session,
    ) -> ToolCatalogSnapshot | ToolExposureSnapshot:
        catalog = self._tool_catalog.snapshot()
        if self._tool_search_mode is None:
            return catalog

        discoveries = restored_discoveries(session.conversation)
        tools = ToolExposureSnapshot.build(catalog, self._tool_search_mode, discoveries)
        invalidated = tools.invalidated(discoveries)
        if invalidated:
            session.append_attachment(ToolDiscoveryInvalidationAttachment(invalidated))
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
            session.append_attachment(ToolSearchListingAttachment(searchable_names))
        return tools

    async def _stream_tool_round(
        self,
        session: Session,
        assistant_message: AssistantMessage,
        tool_calls: tuple[ToolCall, ...],
        tools: ToolCatalogSnapshot | ToolExposureSnapshot,
    ) -> AsyncIterator[AgentEvent]:
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
            session.commit_tool_round(result_message, round_attachments)
            if round_permission_updates:
                self._tool_round.apply_permission_updates(
                    round_permission_updates,
                    lambda mode: session.set_permission_mode(mode.value),
                )
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
            session.commit_tool_round(result_message, round_attachments)
            if round_permission_updates:
                self._tool_round.apply_permission_updates(
                    round_permission_updates,
                    lambda mode: session.set_permission_mode(mode.value),
                )
            yield AgentConversationUpdated()
            raise
        if round_cancelled:
            raise asyncio.CancelledError

    async def _compact_for_retry(
        self,
        session: Session,
        runtime: ContextRuntime,
        trigger: CompactTrigger,
    ) -> CompactionOutcome:
        """在当前 turn 内生成并提交 auto/reactive compact。"""

        outcome = await self._context.compact(
            session.context_planning_state(), trigger, recorder=session
        )
        session.commit_compaction(
            outcome.replacements, outcome.summary, outcome.boundary
        )
        return outcome

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


def _prepared_injections(
    plan: ContextPlan,
    refs: tuple[str, ...],
    previous_refs: set[str],
    *,
    origins: tuple[ModelInputOrigin, ...],
) -> tuple[PreparedContextItem, ...]:
    from my_code.model.request import InputText, UserInput

    items: list[PreparedContextItem] = []
    for model_item, origin, audit_id in zip(
        plan.request.input, origins, refs, strict=True
    ):
        if audit_id in previous_refs or origin.kind not in {
            ModelInputOriginKind.USER_CONTEXT,
            ModelInputOriginKind.ATTACHMENT,
            ModelInputOriginKind.CONTENT_REPLACEMENT,
        }:
            continue
        text = (
            "\n".join(
                block.text
                for block in model_item.content
                if isinstance(block, InputText)
            )
            if isinstance(model_item, UserInput)
            else ""
        )
        items.append(
            PreparedContextItem(
                audit_id,
                origin.source or origin.kind.value,
                origin.attachment_kind,
                text,
            )
        )
    return tuple(items)


__all__ = [
    "AgentEngine",
]
