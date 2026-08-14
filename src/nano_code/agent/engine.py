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
from nano_code.context import (
    ContentReplacement,
    ContextBudget,
    ContextOverflow,
    ContextPlan,
    ContextPlanner,
    ConversationSnapshot,
)
from nano_code.context.compaction import CompactionService
from nano_code.messages import (
    ChatMessage,
    TextBlock,
    TokenUsage,
    ToolResultBlock,
    ToolUseBlock,
)
from nano_code.providers import (
    ModelContextOverflow,
    ModelProvider,
    ModelResponseCompleted,
    ModelTextDelta,
    StreamingModelProvider,
)
from nano_code.sessions import CompactBoundary, CompactTrigger, SessionStore
from nano_code.tools.executor import ToolExecutor


class AgentEngine:
    """管理消息历史，并将 provider、工具和持久化职责委托给对应组件。"""

    def __init__(
        self,
        provider: ModelProvider,
        tool_executor: ToolExecutor,
        session_store: SessionStore,
        context_planner: ContextPlanner,
        compaction_service: CompactionService,
        *,
        max_turns: int = 12,
    ) -> None:
        if max_turns < 1:
            raise ValueError("max_turns must be positive")
        self.provider = provider
        self.tool_executor = tool_executor
        self.session_store = session_store
        self.context_planner = context_planner
        self.compaction_service = compaction_service
        self.max_turns = max_turns

        # 会话记录是持久化事实来源。接受新输入前先修复中断的协议轮次，
        # 避免恢复时携带 provider 会拒绝的孤立 tool_use。
        loaded = session_store.load()
        self.messages = list(session_store.load_working_set(loaded))
        self.content_replacements = _active_replacements(
            session_store.load_content_replacements(), self.messages
        )
        self._repair_trailing_tool_uses()

    def resume(self, session_store: SessionStore) -> tuple[ChatMessage, ...]:
        """校验并恢复另一会话；失败时保持当前引擎状态不变。"""

        full_history = list(session_store.load())
        if not full_history:
            raise ValueError(
                f"Session contains no messages: {session_store.session_id}"
            )
        repairs = _trailing_tool_repairs(full_history)
        if repairs is not None:
            repair_message = ChatMessage(
                role="user",
                origin="tool",
                content=repairs,
                parent_uuid=full_history[-1].uuid,
                source_message_uuid=full_history[-1].uuid,
            )
            # 先完成目标会话的恢复写入，再切换内存状态，确保失败不会留下
            # 一半属于旧会话、一半属于新会话的引擎。
            session_store.append(repair_message)
            full_history.append(repair_message)

        self.session_store = session_store
        self.messages = list(session_store.load_working_set(tuple(full_history)))
        self.content_replacements = _active_replacements(
            session_store.load_content_replacements(), self.messages
        )
        return tuple(full_history)

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
            try:
                request = self.context_planner.plan(self._context_snapshot())
            except ContextOverflow:
                await self.compact("auto")
                request = self.context_planner.plan(self._context_snapshot())
            self._commit_context_plan(request)
            reactive_attempted = False
            while True:
                response = None
                streamed_text = False
                try:
                    if isinstance(self.provider, StreamingModelProvider):
                        async for event in self.provider.stream(request):
                            if isinstance(event, ModelTextDelta):
                                streamed_text = True
                                yield AgentTextDelta(event.text)
                            elif isinstance(event, ModelResponseCompleted):
                                response = event.response
                    else:
                        response = await self.provider.complete(request)
                except ModelContextOverflow:
                    if reactive_attempted:
                        raise
                    reactive_attempted = True
                    await self.compact("reactive")
                    request = self.context_planner.plan(self._context_snapshot())
                    self._commit_context_plan(request)
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

            assistant_message = ChatMessage(
                role="assistant",
                origin="model",
                content=response.content,
                parent_uuid=self._last_uuid,
                usage=response.usage,
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
                    use_presentation = self.tool_executor.present_use(call)
                    yield AgentToolStarted(
                        call.id, call.name, call.input, use_presentation
                    )
                    outcome = await self.tool_executor.execute(call)
                    results.append(outcome.result)
                    yield AgentToolFinished(
                        call.id,
                        call.name,
                        outcome.result.is_error,
                        outcome.presentation,
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
                        presentation=self.tool_executor.present_error(
                            call, "Tool execution was cancelled."
                        ),
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

    async def compact(self, trigger: CompactTrigger = "manual") -> CompactBoundary:
        """持久化摘要边界，并把运行时切换到压缩后的工作集。"""

        if not self.messages:
            raise ValueError("Cannot compact an empty conversation")
        model_messages, replacements = self.context_planner.compaction_view(
            self._context_snapshot()
        )
        for replacement in replacements:
            self.session_store.append_content_replacement(replacement)
            self.content_replacements[replacement.tool_use_id] = replacement

        result = await self.compaction_service.summarize(model_messages)
        parent_uuid = self.messages[-1].uuid
        summary_message = ChatMessage(
            role="user",
            origin="system",
            content=(
                TextBlock(
                    f"<conversation-summary>\n{result.summary}\n</conversation-summary>"
                ),
            ),
            parent_uuid=parent_uuid,
        )
        boundary = CompactBoundary(
            parent_uuid=parent_uuid,
            summary_uuid=summary_message.uuid,
            trigger=trigger,
            pre_compact_chars=self.context_planner.window.size(tuple(self.messages)),
        )
        self.session_store.append_compact_boundary(boundary)
        self._append(summary_message)
        # Transcript 保留完整父链；运行时只需 summary 之后的模型工作集。
        self.messages = [summary_message]
        self.content_replacements = {}
        return boundary

    def context_budget(self) -> ContextBudget:
        """返回当前工作集的只读预算报告。"""

        return self.context_planner.inspect(self._context_snapshot())

    def _commit_context_plan(self, plan: ContextPlan) -> None:
        """在请求发出前持久化影响模型前缀的所有新决策。"""

        for replacement in plan.new_content_replacements:
            self.session_store.append_content_replacement(replacement)
            self.content_replacements[replacement.tool_use_id] = replacement

    def _context_snapshot(self) -> ConversationSnapshot:
        return ConversationSnapshot(
            messages=tuple(self.messages),
            content_replacements=tuple(self.content_replacements.values()),
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


def _active_replacements(
    replacements: tuple[ContentReplacement, ...],
    messages: list[ChatMessage],
) -> dict[str, ContentReplacement]:
    """只把当前 compact 工作集仍引用的替换加载进内存。"""

    tool_ids = {
        block.tool_use_id
        for message in messages
        for block in message.content
        if isinstance(block, ToolResultBlock)
    }
    return {
        replacement.tool_use_id: replacement
        for replacement in replacements
        if replacement.tool_use_id in tool_ids
    }
