"""Anthropic Messages API 适配器。"""

from collections.abc import AsyncIterator, Iterable
from typing import cast

from anthropic import AsyncAnthropic, BadRequestError
from anthropic.types import (
    Message,
    MessageParam,
    TextBlockParam,
    ToolParam,
    ToolResultBlockParam,
    ToolUseBlockParam,
)

from nano_code.agent.contracts.context import ContextPlan
from nano_code.agent.contracts.model import (
    ModelInputMessage,
    ModelResponseCompleted,
    ModelStreamEvent,
    ModelTextDelta,
)
from nano_code.agent.errors import ModelContextOverflow
from nano_code.agent.ports.model import ModelCompletionPort
from nano_code.messages import (
    ModelResponse,
    TextBlock,
    TokenUsage,
    ToolResultBlock,
    ToolUseBlock,
)
from nano_code.messages.models import to_json_object
from nano_code.prompts import PromptStability, SystemPrompt
from nano_code.providers.base import ProviderCapabilities


class AnthropicProvider(ModelCompletionPort):
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
        # 自定义 endpoint 对 Anthropic 扩展字段的实现程度未知，默认保持兼容。
        self._capabilities = self.capabilities_for(base_url)

    @staticmethod
    def capabilities_for(base_url: str | None) -> ProviderCapabilities:
        """官方 endpoint 支持缓存；兼容线路由各自适配器后续声明。"""

        if base_url is None:
            return ProviderCapabilities(
                system_prompt_blocks=True,
                prompt_caching=True,
                max_prompt_cache_breakpoints=2,
            )
        return ProviderCapabilities()

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def close(self) -> None:
        """释放 SDK 底层 HTTP 客户端。"""

        await self.client.close()

    async def complete(self, request: ContextPlan) -> ModelResponse:
        try:
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=request.max_output_tokens,
                system=self._system(request.system_prompt),
                messages=self._request_messages(request),
                tools=self._tools(request),
            )
        except BadRequestError as error:
            _raise_context_overflow(error)
            raise
        if not isinstance(response, Message):
            raise TypeError("Expected a non-streaming Anthropic Message")
        return self._response(response)

    async def stream(self, request: ContextPlan) -> AsyncIterator[ModelStreamEvent]:
        """流式输出展示文本，随后发出经 SDK 校验的最终快照。"""

        try:
            async with self.client.messages.stream(
                model=self.model,
                max_tokens=request.max_output_tokens,
                system=self._system(request.system_prompt),
                messages=self._request_messages(request),
                tools=self._tools(request),
            ) as stream:
                async for event in stream:
                    if (
                        event.type == "content_block_delta"
                        and event.delta.type == "text_delta"
                    ):
                        yield ModelTextDelta(event.delta.text)
                final_message = cast(Message, await stream.get_final_message())
        except BadRequestError as error:
            _raise_context_overflow(error)
            raise
        yield ModelResponseCompleted(response=self._response(final_message))

    @staticmethod
    def _messages(messages: Iterable[ModelInputMessage]) -> list[MessageParam]:
        # 上下文层已经移除了 Transcript 元数据并校验协议；适配器只转换 SDK 类型。
        normalized: list[MessageParam] = []
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
            normalized.append({"role": message.role, "content": content})
        return normalized

    @staticmethod
    def _request_messages(request: ContextPlan) -> list[MessageParam]:
        """Place non-history workspace context before the conversation history."""

        return AnthropicProvider._messages(
            (*request.workspace_context, *request.messages)
        )

    def _system(self, prompt: SystemPrompt) -> str | list[TextBlockParam]:
        """按 provider 自身能力消费核心提供的稳定性信息。"""

        return _system_prompt_param(prompt, self.capabilities)

    @staticmethod
    def _tools(request: ContextPlan) -> list[ToolParam]:
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
                cache_creation_input_tokens=(
                    response.usage.cache_creation_input_tokens or 0
                ),
                cache_read_input_tokens=response.usage.cache_read_input_tokens or 0,
            ),
        )


def _system_prompt_param(
    prompt: SystemPrompt,
    capabilities: ProviderCapabilities,
) -> str | list[TextBlockParam]:
    """将稳定片段映射到 Anthropic system 参数，便于独立验证。"""

    if not capabilities.prompt_caching:
        return prompt.text

    blocks: list[TextBlockParam] = [
        {"type": "text", "text": section.content} for section in prompt.sections
    ]
    boundaries: list[int] = []
    for stability in (PromptStability.STATIC, PromptStability.SESSION):
        matching = [
            index
            for index, section in enumerate(prompt.sections)
            if section.stability is stability
        ]
        if matching:
            boundaries.append(matching[-1])
    for index in boundaries[: capabilities.max_prompt_cache_breakpoints]:
        blocks[index]["cache_control"] = {"type": "ephemeral"}
    return blocks


def _raise_context_overflow(error: BadRequestError) -> None:
    """只把明确的上下文长度错误映射为可恢复核心错误。"""

    message = str(error).casefold()
    markers = (
        "prompt is too long",
        "context window",
        "context_length_exceeded",
        "too many tokens",
        "input length",
    )
    if any(marker in message for marker in markers):
        raise ModelContextOverflow("Model context window exceeded") from error
