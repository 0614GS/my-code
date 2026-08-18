"""Anthropic Messages API 适配器。"""

from collections.abc import AsyncIterator, Iterable
from typing import Any, Literal, cast

from anthropic import AsyncAnthropic, BadRequestError
from anthropic.types import (
    Message,
    MessageParam,
    RedactedThinkingBlockParam,
    TextBlockParam,
    ThinkingBlockParam,
    ToolParam,
    ToolResultBlockParam,
    ToolUseBlockParam,
)

from nano_code.agent.contracts.model import (
    ModelMessage,
    ModelOutput,
    ModelOutputCompleted,
    ModelReasoningBlock,
    ModelReasoningCompleted,
    ModelReasoningDelta,
    ModelReasoningStarted,
    ModelRequest,
    ModelStreamEvent,
    ModelTextBlock,
    ModelTextCompleted,
    ModelTextDelta,
    ModelTextStarted,
    ModelToolResultBlock,
    ModelToolUseBlock,
)
from nano_code.agent.errors import ModelContextOverflow
from nano_code.agent.ports.model import ModelCompletionPort
from nano_code.conversation import (
    ProviderBinding,
    ProviderContinuationState,
    ReasoningPresentation,
    TokenUsage,
    to_json_object,
)
from nano_code.conversation.primitives import new_id
from nano_code.prompts import PromptStability, SystemPrompt
from nano_code.providers.base import ProviderCapabilities
from nano_code.providers.profiles import ReasoningConfig
from nano_code.providers.streaming import ModelStreamSequencer


class AnthropicProvider(ModelCompletionPort):
    """转换内部消息，同时不让 SDK 类型泄漏到核心层。"""

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        provider_id: str = "anthropic",
        reasoning: ReasoningConfig | None = None,
    ) -> None:
        self.model = model
        self.binding = ProviderBinding(
            "anthropic-messages", provider_id, model, base_url
        )
        self.reasoning = reasoning or ReasoningConfig(enabled=False)
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

    async def complete(self, request: ModelRequest) -> ModelOutput:
        try:
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=request.max_output_tokens,
                system=self._system(request.system_prompt),
                messages=self._messages(request.messages, binding=self.binding),
                tools=self._tools(request),
                **cast(Any, self._reasoning_params()),
            )
        except BadRequestError as error:
            _raise_context_overflow(error)
            raise
        if not isinstance(response, Message):
            raise TypeError("Expected a non-streaming Anthropic Message")
        return self._response(response)

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        """流式输出展示文本，随后发出经 SDK 校验的最终快照。"""

        sequencer = ModelStreamSequencer()
        started: set[int] = set()
        completed: set[int] = set()
        kinds: dict[int, str] = {}
        text_parts: dict[int, str] = {}
        thinking_parts: dict[int, str] = {}
        try:
            async with self.client.messages.stream(
                model=self.model,
                max_tokens=request.max_output_tokens,
                system=self._system(request.system_prompt),
                messages=self._messages(request.messages, binding=self.binding),
                tools=self._tools(request),
                **cast(Any, self._reasoning_params()),
            ) as stream:
                async for event in stream:
                    if event.type == "content_block_start":
                        index = event.index
                        kind = event.content_block.type
                        kinds[index] = kind
                        if kind == "text":
                            started.add(index)
                            text_parts[index] = str(
                                getattr(event.content_block, "text", "")
                            )
                            yield sequencer.emit(ModelTextStarted())
                        elif kind in {"thinking", "redacted_thinking"}:
                            started.add(index)
                            thinking_parts[index] = str(
                                getattr(event.content_block, "thinking", "")
                            )
                            disclosure = (
                                "verbatim" if kind == "thinking" else "redacted"
                            )
                            yield sequencer.emit(
                                ModelReasoningStarted(
                                    cast(Literal["verbatim", "redacted"], disclosure)
                                )
                            )
                    elif event.type == "content_block_delta" and (
                        event.delta.type == "text_delta"
                    ):
                        index = event.index
                        if index not in started:
                            started.add(index)
                            kinds[index] = "text"
                            text_parts[index] = ""
                            yield sequencer.emit(ModelTextStarted())
                        text_parts[index] = text_parts.get(index, "") + event.delta.text
                        yield sequencer.emit(ModelTextDelta(event.delta.text))
                    elif (
                        event.type == "content_block_delta"
                        and event.delta.type == "thinking_delta"
                    ):
                        index = event.index
                        if index not in started:
                            started.add(index)
                            kinds[index] = "thinking"
                            thinking_parts[index] = ""
                            yield sequencer.emit(ModelReasoningStarted("verbatim"))
                        thinking_parts[index] = (
                            thinking_parts.get(index, "") + event.delta.thinking
                        )
                        yield sequencer.emit(
                            ModelReasoningDelta("verbatim", 0, event.delta.thinking)
                        )
                    elif event.type == "content_block_stop":
                        index = event.index
                        stopped_kind = kinds.get(index)
                        if stopped_kind == "text":
                            completed.add(index)
                            yield sequencer.emit(
                                ModelTextCompleted(text_parts.get(index, ""))
                            )
                        elif stopped_kind == "thinking":
                            completed.add(index)
                            thinking = thinking_parts.get(index, "")
                            presentation = (
                                ReasoningPresentation("verbatim", (thinking,))
                                if thinking
                                else ReasoningPresentation("hidden")
                            )
                            yield sequencer.emit(ModelReasoningCompleted(presentation))
                        elif stopped_kind == "redacted_thinking":
                            completed.add(index)
                            yield sequencer.emit(
                                ModelReasoningCompleted(
                                    ReasoningPresentation("redacted")
                                )
                            )
                final_message = cast(Message, await stream.get_final_message())
        except BadRequestError as error:
            _raise_context_overflow(error)
            raise
        output = self._response(final_message)
        for index, block in enumerate(output.content):
            if not isinstance(block, (ModelTextBlock, ModelReasoningBlock)):
                continue
            if index not in started:
                if isinstance(block, ModelTextBlock):
                    yield sequencer.emit(ModelTextStarted())
                else:
                    yield sequencer.emit(
                        ModelReasoningStarted(block.presentation.disclosure)
                    )
            if index not in completed:
                if isinstance(block, ModelTextBlock):
                    yield sequencer.emit(ModelTextCompleted(block.text))
                else:
                    yield sequencer.emit(ModelReasoningCompleted(block.presentation))
        yield sequencer.emit(ModelOutputCompleted(output=output))

    @staticmethod
    def _messages(
        messages: Iterable[ModelMessage],
        *,
        binding: ProviderBinding | None = None,
        model: str | None = None,
    ) -> list[MessageParam]:
        # 上下文层已经移除了 Transcript 元数据并校验协议；适配器只转换 SDK 类型。
        normalized: list[MessageParam] = []
        for message in messages:
            content: list[
                TextBlockParam
                | ToolUseBlockParam
                | ToolResultBlockParam
                | ThinkingBlockParam
                | RedactedThinkingBlockParam
            ] = []
            for block in message.content:
                if isinstance(block, ModelTextBlock):
                    content.append({"type": "text", "text": block.text})
                elif isinstance(block, ModelToolUseBlock):
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
                elif isinstance(block, ModelToolResultBlock):
                    content.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.tool_use_id,
                            "content": block.content,
                            "is_error": block.is_error,
                        }
                    )
                elif (
                    isinstance(block, ModelReasoningBlock)
                    and block.continuation is not None
                    and _binding_matches(
                        block.continuation.binding,
                        binding
                        or ProviderBinding(
                            "anthropic-messages", "anthropic", model or "unknown"
                        ),
                    )
                ):
                    payload = _anthropic_payload(block.continuation)
                    if payload["type"] == "thinking":
                        content.append(cast(ThinkingBlockParam, dict(payload)))
                    else:
                        content.append(cast(RedactedThinkingBlockParam, dict(payload)))
            normalized.append({"role": message.role, "content": content})
        return normalized

    def _system(self, prompt: SystemPrompt) -> str | list[TextBlockParam]:
        """按 provider 自身能力消费核心提供的稳定性信息。"""

        return _system_prompt_param(prompt, self.capabilities)

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

    def _response(self, response: Message) -> ModelOutput:
        content: list[ModelTextBlock | ModelToolUseBlock | ModelReasoningBlock] = []
        for block in response.content:
            if block.type == "thinking":
                payload: dict[str, Any] = {
                    "type": "thinking",
                    "thinking": block.thinking,
                    "signature": block.signature,
                }
                content.append(
                    ModelReasoningBlock(
                        new_id(),
                        (
                            ReasoningPresentation("verbatim", (block.thinking,))
                            if block.thinking
                            else ReasoningPresentation("hidden")
                        ),
                        ProviderContinuationState(
                            self.binding, "active_trajectory", payload
                        ),
                    )
                )
            elif block.type == "redacted_thinking":
                content.append(
                    ModelReasoningBlock(
                        new_id(),
                        ReasoningPresentation("redacted"),
                        ProviderContinuationState(
                            self.binding,
                            "active_trajectory",
                            {"type": "redacted_thinking", "data": block.data},
                        ),
                    )
                )
            elif block.type == "text":
                content.append(ModelTextBlock(text=block.text))
            elif block.type == "tool_use":
                content.append(
                    ModelToolUseBlock(
                        id=block.id,
                        name=block.name,
                        input=to_json_object(block.input),
                    )
                )
        return ModelOutput(
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

    def _reasoning_params(self) -> dict[str, object]:
        if not self.reasoning.enabled:
            return {}
        params: dict[str, object] = {"thinking": {"type": "adaptive"}}
        if self.reasoning.effort != "auto":
            params["output_config"] = {"effort": self.reasoning.effort}
        return params


def _binding_matches(actual: ProviderBinding, expected: ProviderBinding) -> bool:
    return actual == expected


def _anthropic_payload(state: ProviderContinuationState) -> dict[str, object]:
    payload = state.payload
    kind = payload.get("type")
    if kind == "thinking":
        valid = set(payload) == {"type", "thinking", "signature"} and all(
            isinstance(payload.get(key), str) for key in ("thinking", "signature")
        )
    elif kind == "redacted_thinking":
        valid = set(payload) == {"type", "data"} and isinstance(
            payload.get("data"), str
        )
    else:
        valid = False
    if not valid:
        raise ValueError("Invalid Anthropic continuation payload")
    return cast(dict[str, object], dict(payload))


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
