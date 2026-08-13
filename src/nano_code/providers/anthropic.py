"""Anthropic Messages API 适配器。"""

from collections.abc import AsyncIterator, Iterable
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
from nano_code.providers.events import (
    ModelResponseCompleted,
    ModelStreamEvent,
    ModelTextDelta,
)


class AnthropicProvider:
    """转换内部消息，同时不让 SDK 类型泄漏到核心层。"""

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.model = model
        self.client = AsyncAnthropic(api_key=api_key, base_url=base_url)

    async def close(self) -> None:
        """释放 SDK 底层 HTTP 客户端。"""

        await self.client.close()

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

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        """流式输出展示文本，随后发出经 SDK 校验的最终快照。"""

        async with self.client.messages.stream(
            model=self.model,
            max_tokens=request.max_output_tokens,
            system=request.system_prompt,
            messages=self._messages(request.messages),
            tools=self._tools(request),
        ) as stream:
            async for event in stream:
                if (
                    event.type == "content_block_delta"
                    and event.delta.type == "text_delta"
                ):
                    yield ModelTextDelta(event.delta.text)
            final_message = cast(Message, await stream.get_final_message())
        yield ModelResponseCompleted(self._response(final_message))

    @staticmethod
    def _messages(messages: Iterable[ChatMessage]) -> list[MessageParam]:
        # 这里是 API 投影边界。内部 UUID、时间戳和来源信息只留在会话记录中，
        # 绝不发送给 provider。
        projected: list[MessageParam] = []
        for message in messages:
            content: list[
                TextBlockParam | ToolUseBlockParam | ToolResultBlockParam
            ] = []
            for block in message.content:
                if isinstance(block, TextBlock):
                    content.append({"type": "text", "text": block.text})
                elif isinstance(block, ToolUseBlock):
                    # JsonObject 递归地比 SDK object 类型更窄；此 cast 只改变
                    # 静态类型变体，不改变运行时数据。
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
        # 定义按注册表顺序到达。此处不要重新排序，因为工具 schema 顺序会影响
        # provider 可缓存的提示前缀。
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
        # thinking 和服务端工具块不在首个 MVP 范围内。如果响应不含受支持的
        # text/tool_use 块，ModelResponse 会显式失败，而不是持久化丢失信息的空消息。
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
