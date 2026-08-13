"""The model → tools → model loop for one persisted user turn."""

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
    """Own message history while delegating provider, tools, and persistence."""

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

        # The transcript is the durable source of truth. Repair an interrupted
        # protocol round before accepting new input so resume never starts with a
        # dangling tool_use that the provider would reject.
        self.messages = list(session_store.load())
        self._repair_trailing_tool_uses()

    async def submit(self, prompt: str) -> AgentTurnResult:
        """Consume the observable loop and return its terminal value."""

        completed: AgentTurnResult | None = None
        async for event in self.submit_stream(prompt):
            if isinstance(event, AgentTurnCompleted):
                completed = event.result
        if completed is None:
            raise RuntimeError("Agent stream ended without a completed turn")
        return completed

    async def submit_stream(self, prompt: str) -> AsyncIterator[AgentEvent]:
        """Run one turn while exposing ephemeral text and tool lifecycle events."""

        if not prompt.strip():
            raise ValueError("Prompt must not be empty")

        user_message = ChatMessage(
            role="user",
            origin="human",
            content=(TextBlock(prompt),),
            parent_uuid=self._last_uuid,
        )

        # Persist before the first API request. A crash or network failure must not
        # erase the prompt that initiated the turn.
        self._append(user_message)

        input_tokens = 0
        output_tokens = 0
        final_text = ""
        for turn in range(1, self.max_turns + 1):
            # Keep the full transcript for recovery, but derive a bounded API view
            # independently on every iteration.
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

            # Store the model's tool requests before executing them. This makes an
            # interrupted process observable and repairable on the next resume.
            self._append(assistant_message)
            final_text = "\n".join(
                block.text for block in response.content if isinstance(block, TextBlock)
            ).strip()
            tool_calls = tuple(
                block for block in response.content if isinstance(block, ToolUseBlock)
            )

            # Absence of tool calls is the explicit terminal condition for this MVP.
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
                # Calls are intentionally serial in the MVP. Parallel execution must
                # later honor each tool's concurrency and context-mutation contract.
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
                # Anthropic's protocol requires one result for every emitted tool_use,
                # including calls that never started because the turn was cancelled.
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
        # Durable-first ordering prevents in-memory history from getting ahead of
        # the transcript if a write fails.
        self.session_store.append(message)
        self.messages.append(message)

    def _append_tool_results(
        self, results: list[ToolResultBlock], assistant_uuid: str
    ) -> None:
        # Keep all results from one model response in a single user-role message.
        # Inserting ordinary user text between tool_use and tool_result is invalid.
        tool_message = ChatMessage(
            role="user",
            origin="tool",
            content=tuple(results),
            parent_uuid=self._last_uuid,
            source_message_uuid=assistant_uuid,
        )
        self._append(tool_message)

    def _repair_trailing_tool_uses(self) -> None:
        """Close a tool protocol round left incomplete by a prior process exit."""

        if not self.messages:
            return
        trailing = self.messages[-1]
        if trailing.role != "assistant":
            return
        calls = [block for block in trailing.content if isinstance(block, ToolUseBlock)]
        if not calls:
            return

        # A synthetic error is safer than replaying a possibly side-effecting tool:
        # the previous process may have completed the effect just before it exited.
        repairs = [
            ToolResultBlock(
                tool_use_id=call.id,
                content="Tool execution was interrupted before the session resumed.",
                is_error=True,
            )
            for call in calls
        ]
        self._append_tool_results(repairs, trailing.uuid)
