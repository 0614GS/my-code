"""Official OpenAI Responses API stateless adapter."""

import json
from collections.abc import AsyncIterator, Iterable
from typing import Any, cast

from openai import AsyncOpenAI, BadRequestError

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
    ModelTextDelta,
    ModelToolResultBlock,
    ModelToolUseBlock,
)
from nano_code.agent.errors import ModelContextOverflow
from nano_code.agent.ports.model import ModelCompletionPort
from nano_code.conversation import (
    JsonObject,
    ProviderBinding,
    ProviderContinuationState,
    ReasoningPresentation,
    TokenUsage,
    to_json_object,
)
from nano_code.providers.base import ProviderCapabilities
from nano_code.providers.profiles import ReasoningConfig


class OpenAIResponsesProvider(ModelCompletionPort):
    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        provider_id: str = "openai",
        reasoning: ReasoningConfig | None = None,
    ) -> None:
        self.model = model
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.binding = ProviderBinding("openai-responses", provider_id, model, base_url)
        self.reasoning = reasoning or ReasoningConfig()

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities()

    async def close(self) -> None:
        await self.client.close()

    async def complete(self, request: ModelRequest) -> ModelOutput:
        try:
            response = await self.client.responses.create(
                **cast(Any, self._request_params(request))
            )
        except BadRequestError as error:
            _raise_context_overflow(error)
            raise
        return self._response(response)

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        params = self._request_params(request)
        params["stream"] = True
        final: object | None = None
        reasoning_ids: dict[int, str] = {}
        started_reasoning: set[int] = set()
        try:
            stream = await self.client.responses.create(**cast(Any, params))
            async for event in cast(Any, stream):
                event_type = getattr(event, "type", "")
                if event_type == "response.output_text.delta":
                    yield ModelTextDelta(str(event.delta))
                elif (
                    event_type == "response.output_item.added"
                    and getattr(event.item, "type", None) == "reasoning"
                ):
                    index = int(event.output_index)
                    reasoning_id = str(
                        getattr(event.item, "id", "") or f"openai:{index}"
                    )
                    reasoning_ids[index] = reasoning_id
                elif event_type == "response.reasoning_summary_text.delta":
                    index = int(event.output_index)
                    reasoning_id = reasoning_ids.setdefault(
                        index, str(getattr(event, "item_id", "") or f"openai:{index}")
                    )
                    if index not in started_reasoning:
                        started_reasoning.add(index)
                        yield ModelReasoningStarted(reasoning_id, "summary")
                    yield ModelReasoningDelta(
                        reasoning_id,
                        "summary",
                        int(getattr(event, "summary_index", 0)),
                        str(event.delta),
                    )
                elif (
                    event_type == "response.output_item.done"
                    and int(event.output_index) in reasoning_ids
                ):
                    index = int(event.output_index)
                    if index not in started_reasoning:
                        started_reasoning.add(index)
                        yield ModelReasoningStarted(reasoning_ids[index], "hidden")
                    yield ModelReasoningCompleted(reasoning_ids[index])
                elif event_type in {"response.completed", "response.incomplete"}:
                    final = event.response
                elif event_type == "response.failed":
                    raise RuntimeError(f"OpenAI response failed: {event.response}")
        except BadRequestError as error:
            _raise_context_overflow(error)
            raise
        if final is None:
            raise RuntimeError("OpenAI Responses stream ended without a final response")
        yield ModelOutputCompleted(self._response(final))

    def _request_params(self, request: ModelRequest) -> dict[str, object]:
        reasoning: dict[str, object] = {
            "summary": "auto",
            "context": self.reasoning.context,
        }
        if self.reasoning.effort != "auto":
            reasoning["effort"] = self.reasoning.effort
        params: dict[str, object] = {
            "model": self.model,
            "store": False,
            "instructions": request.system_prompt.text,
            "input": self._input(request.messages),
            "tools": [
                {
                    "type": "function",
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                    "strict": False,
                }
                for tool in request.tools
            ],
            "max_output_tokens": request.max_output_tokens,
            "include": ["reasoning.encrypted_content"],
        }
        if self.reasoning.enabled:
            params["reasoning"] = reasoning
        return params

    def _input(self, messages: Iterable[ModelMessage]) -> list[object]:
        materialized = tuple(messages)
        result: list[object] = []
        for message in materialized:
            for block in message.content:
                continuation = getattr(block, "continuation", None)
                if continuation is not None and continuation.binding == self.binding:
                    result.append(_openai_item(continuation))
                    continue
                if isinstance(block, ModelTextBlock):
                    result.append({"role": message.role, "content": block.text})
                elif isinstance(block, ModelToolUseBlock):
                    result.append(
                        {
                            "type": "function_call",
                            "call_id": block.id,
                            "name": block.name,
                            "arguments": json.dumps(
                                block.input, ensure_ascii=False, separators=(",", ":")
                            ),
                        }
                    )
                elif isinstance(block, ModelToolResultBlock):
                    result.append(
                        {
                            "type": "function_call_output",
                            "call_id": block.tool_use_id,
                            "output": block.content,
                        }
                    )
        return result

    def _response(self, response: object) -> ModelOutput:
        content: list[ModelTextBlock | ModelToolUseBlock | ModelReasoningBlock] = []
        for index, item in enumerate(getattr(response, "output", ())):
            payload = _model_dump(item)
            item_type = payload.get("type")
            if item_type in {"reasoning", "function_call", "message"}:
                _validate_openai_payload(payload)
            continuation = ProviderContinuationState(
                self.binding, "working_context", payload
            )
            if item_type == "reasoning":
                parts = tuple(
                    str(part["text"])
                    for part in cast(list[object], payload.get("summary", []))
                    if isinstance(part, dict)
                    and part.get("type") == "summary_text"
                    and isinstance(part.get("text"), str)
                    and part["text"]
                )
                presentation = (
                    ReasoningPresentation("summary", parts)
                    if parts
                    else ReasoningPresentation("hidden")
                )
                content.append(
                    ModelReasoningBlock(
                        str(
                            payload.get("id")
                            or (
                                f"{getattr(response, 'id', 'response')}"
                                f":reasoning:{index}"
                            )
                        ),
                        presentation,
                        continuation,
                    )
                )
            elif item_type == "function_call":
                arguments = payload.get("arguments")
                try:
                    parsed = (
                        json.loads(arguments)
                        if isinstance(arguments, str)
                        else arguments
                    )
                    tool_input = to_json_object(parsed)
                except (json.JSONDecodeError, TypeError) as error:
                    raise ValueError(
                        "Invalid OpenAI function call arguments"
                    ) from error
                call_id = payload.get("call_id")
                name = payload.get("name")
                if not isinstance(call_id, str) or not isinstance(name, str):
                    raise ValueError("Invalid OpenAI function call output item")
                content.append(
                    ModelToolUseBlock(call_id, name, tool_input, continuation)
                )
            elif item_type == "message":
                texts: list[str] = []
                for part in cast(list[object], payload.get("content", [])):
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "output_text" and isinstance(
                        part.get("text"), str
                    ):
                        texts.append(cast(str, part["text"]))
                    elif part.get("type") == "refusal" and isinstance(
                        part.get("refusal"), str
                    ):
                        texts.append(cast(str, part["refusal"]))
                if texts:
                    content.append(ModelTextBlock("\n".join(texts), continuation))
        usage = getattr(response, "usage", None)
        cached_tokens = int(
            getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", 0)
            or 0
        )
        total_input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        return ModelOutput(
            tuple(content),
            str(getattr(response, "status", "unknown")),
            TokenUsage(
                input_tokens=max(0, total_input_tokens - cached_tokens),
                output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
                cache_read_input_tokens=cached_tokens,
            ),
        )


def _model_dump(item: object) -> JsonObject:
    dump = getattr(item, "model_dump", None)
    if not callable(dump):
        raise TypeError("Expected an OpenAI response output item")
    return to_json_object(dump(mode="json", exclude_none=False))


def _openai_item(state: ProviderContinuationState) -> JsonObject:
    payload = state.payload
    _validate_openai_payload(payload)
    return to_json_object(payload)


def _validate_openai_payload(payload: JsonObject) -> None:
    kind = payload.get("type")
    if kind not in {"reasoning", "function_call", "message"}:
        raise ValueError("Invalid OpenAI continuation output item")
    if not isinstance(payload.get("id"), str):
        raise ValueError("OpenAI continuation item requires an id")
    if kind == "reasoning" and not isinstance(payload.get("summary"), list):
        raise ValueError("Invalid OpenAI reasoning continuation")
    if kind == "function_call" and not all(
        isinstance(payload.get(key), str) for key in ("call_id", "name", "arguments")
    ):
        raise ValueError("Invalid OpenAI function call continuation")
    if kind == "message" and (
        payload.get("role") != "assistant"
        or not isinstance(payload.get("content"), list)
    ):
        raise ValueError("Invalid OpenAI message continuation")


def _raise_context_overflow(error: BadRequestError) -> None:
    message = str(error).casefold()
    if any(
        marker in message
        for marker in ("context_length_exceeded", "context window", "too many tokens")
    ):
        raise ModelContextOverflow("Model context window exceeded") from error


__all__ = ["OpenAIResponsesProvider"]
