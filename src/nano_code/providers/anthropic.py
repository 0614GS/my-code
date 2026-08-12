"""Anthropic Messages API adapter."""

from collections.abc import Iterable
from typing import cast

from anthropic import AsyncAnthropic
from anthropic.types import (
    Message,
    MessageParam,
    TextBlockParam,
    ToolParam,
    ToolResultBlockParam,
    ToolUseBlockParam,
)

from nano_code.messages import (
    ChatMessage,
    ModelResponse,
    TextBlock,
    TokenUsage,
    ToolResultBlock,
    ToolUseBlock,
)
from nano_code.messages.models import to_json_object
from nano_code.providers.base import ModelRequest


class AnthropicProvider:
    """Translate internal messages without leaking SDK types into the core."""

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.model = model
        self.client = AsyncAnthropic(api_key=api_key, base_url=base_url)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        response = await self.client.messages.create(
            model=self.model,
            max_tokens=request.max_output_tokens,
            system=request.system_prompt,
            messages=self._messages(request.messages),
            tools=self._tools(request),
        )
        if not isinstance(response, Message):
            raise TypeError("Expected a non-streaming Anthropic Message")
        return self._response(response)

    @staticmethod
    def _messages(messages: Iterable[ChatMessage]) -> list[MessageParam]:
        projected: list[MessageParam] = []
        for message in messages:
            content: list[
                TextBlockParam | ToolUseBlockParam | ToolResultBlockParam
            ] = []
            for block in message.content:
                if isinstance(block, TextBlock):
                    content.append({"type": "text", "text": block.text})
                elif isinstance(block, ToolUseBlock):
                    content.append(
                        {
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": cast(dict[str, object], block.input),
                        }
                    )
                elif isinstance(block, ToolResultBlock):
                    content.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.tool_use_id,
                            "content": block.content,
                            "is_error": block.is_error,
                        }
                    )
            projected.append({"role": message.role, "content": content})
        return projected

    @staticmethod
    def _tools(request: ModelRequest) -> list[ToolParam]:
        tools: list[ToolParam] = []
        for definition in request.tools:
            tools.append(
                {
                    "name": definition.name,
                    "description": definition.description,
                    "input_schema": cast(dict[str, object], definition.input_schema),
                }
            )
        return tools

    @staticmethod
    def _response(response: Message) -> ModelResponse:
        content: list[TextBlock | ToolUseBlock] = []
        for block in response.content:
            if block.type == "text":
                content.append(TextBlock(text=block.text))
            elif block.type == "tool_use":
                content.append(
                    ToolUseBlock(
                        id=block.id,
                        name=block.name,
                        input=to_json_object(block.input),
                    )
                )
        return ModelResponse(
            content=tuple(content),
            stop_reason=response.stop_reason or "unknown",
            usage=TokenUsage(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            ),
        )
