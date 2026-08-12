"""The model → tools → model loop for one persisted user turn."""

import asyncio
from dataclasses import dataclass

from nano_code.context import ContextWindow
from nano_code.messages import (
    ChatMessage,
    TextBlock,
    TokenUsage,
    ToolResultBlock,
    ToolUseBlock,
)
from nano_code.providers import ModelProvider, ModelRequest
from nano_code.sessions import SessionStore
from nano_code.tools.executor import ToolExecutor


@dataclass(frozen=True, slots=True)
class AgentTurnResult:
    """Terminal data for one human prompt."""

    text: str
    turns: int
    usage: TokenUsage


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
        self.messages = list(session_store.load())
        self._repair_trailing_tool_uses()

    async def submit(self, prompt: str) -> AgentTurnResult:
        """Persist input before sampling, then run until a terminal response."""

        if not prompt.strip():
            raise ValueError("Prompt must not be empty")

        user_message = ChatMessage(
            role="user",
            origin="human",
            content=(TextBlock(prompt),),
            parent_uuid=self._last_uuid,
        )
        self._append(user_message)

        input_tokens = 0
        output_tokens = 0
        final_text = ""
        for turn in range(1, self.max_turns + 1):
            projected = self.context_window.project(tuple(self.messages))
            response = await self.provider.complete(
                ModelRequest(
                    system_prompt=self.system_prompt,
                    messages=projected,
                    tools=self.tool_executor.registry.definitions,
                    max_output_tokens=self.max_output_tokens,
                )
            )
            input_tokens += response.usage.input_tokens
            output_tokens += response.usage.output_tokens

            assistant_message = ChatMessage(
                role="assistant",
                origin="model",
                content=response.content,
                parent_uuid=self._last_uuid,
            )
            self._append(assistant_message)
            final_text = "\n".join(
                block.text for block in response.content if isinstance(block, TextBlock)
            ).strip()
            tool_calls = tuple(
                block for block in response.content if isinstance(block, ToolUseBlock)
            )
            if not tool_calls:
                return AgentTurnResult(
                    text=final_text,
                    turns=turn,
                    usage=TokenUsage(input_tokens, output_tokens),
                )

            results: list[ToolResultBlock] = []
            try:
                for call in tool_calls:
                    results.append(await self.tool_executor.execute(call))
            except asyncio.CancelledError:
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
        self.session_store.append(message)
        self.messages.append(message)

    def _append_tool_results(
        self, results: list[ToolResultBlock], assistant_uuid: str
    ) -> None:
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
        repairs = [
            ToolResultBlock(
                tool_use_id=call.id,
                content="Tool execution was interrupted before the session resumed.",
                is_error=True,
            )
            for call in calls
        ]
        self._append_tool_results(repairs, trailing.uuid)
