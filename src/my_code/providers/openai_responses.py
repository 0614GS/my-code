"""Official OpenAI Responses API stateless adapter."""

import json
from collections.abc import AsyncIterator, Iterable
from typing import Any, cast
from uuid import uuid4

from openai import AsyncOpenAI, BadRequestError

from my_code.config.providers import ReasoningConfig
from my_code.model.capabilities import ProviderCapabilities
from my_code.model.client import ModelClient
from my_code.model.errors import ModelContextOverflow
from my_code.model.events import (
    ModelOutputCompleted,
    ModelReasoningCompleted,
    ModelReasoningDelta,
    ModelReasoningStarted,
    ModelStreamEvent,
    ModelStreamPayload,
    ModelStreamSequencer,
    ModelTextCompleted,
    ModelTextDelta,
    ModelTextStarted,
)
from my_code.model.primitives import (
    JsonObject,
    ProviderBinding,
    ProviderContinuationState,
    ReasoningPresentation,
    TokenUsage,
    to_json_object,
)
from my_code.model.request import (
    AssistantOutput,
    InputDocument,
    InputImage,
    InputText,
    ModelInputItem,
    ModelOutput,
    ModelReasoningBlock,
    ModelRequest,
    ModelTextBlock,
    ModelToolUseBlock,
    ToolOutput,
    ToolOutputDocument,
    ToolOutputImage,
    ToolOutputs,
    ToolOutputText,
    UserInput,
)

type _DisplayKey = tuple[str, int]


class _OpenAIStreamNormalizer:
    """Serialize provider output items into one active display block."""

    def __init__(self) -> None:
        self.active: _DisplayKey | None = None
        self.started: set[_DisplayKey] = set()
        self.completed: set[_DisplayKey] = set()
        self.pending: list[object] = []

    def feed(self, event: object) -> list[ModelStreamPayload]:
        key = self._key(event)
        if key is None:
            return []
        if self.active is not None and self.active != key:
            self.pending.append(event)
            return []
        payloads = self._process(event, key)
        while self.active is None and self.pending:
            pending = self.pending.pop(0)
            pending_key = self._key(pending)
            if pending_key is None:
                continue
            payloads.extend(self._process(pending, pending_key))
        return payloads

    def reconcile(self, response: object) -> list[ModelStreamPayload]:
        """Close incomplete blocks and synthesize blocks absent from the SSE stream."""

        payloads: list[ModelStreamPayload] = []
        self.pending.clear()
        for index, item in enumerate(getattr(response, "output", ())):
            raw = _model_dump(item)
            kind = raw.get("type")
            if kind == "reasoning":
                key = ("reasoning", index)
                if key in self.completed:
                    continue
                presentation = _reasoning_presentation(raw)
                if key not in self.started:
                    payloads.append(ModelReasoningStarted(presentation.disclosure))
                    self.started.add(key)
                payloads.append(ModelReasoningCompleted(presentation))
                self.completed.add(key)
            elif kind == "message":
                key = ("text", index)
                if key in self.completed:
                    continue
                text = _message_text(raw)
                if not text and key not in self.started:
                    continue
                if key not in self.started:
                    payloads.append(ModelTextStarted())
                    self.started.add(key)
                payloads.append(ModelTextCompleted(text))
                self.completed.add(key)
        self.active = None
        return payloads

    @staticmethod
    def _key(event: object) -> _DisplayKey | None:
        raw_event = cast(Any, event)
        event_type = getattr(raw_event, "type", "")
        if event_type in {
            "response.output_text.delta",
            "response.refusal.delta",
        }:
            return ("text", int(raw_event.output_index))
        if event_type == "response.reasoning_summary_text.delta":
            return ("reasoning", int(raw_event.output_index))
        if event_type == "response.output_item.done":
            item_type = getattr(raw_event.item, "type", None)
            if item_type == "reasoning":
                return ("reasoning", int(raw_event.output_index))
            if item_type == "message":
                return ("text", int(raw_event.output_index))
        return None

    def _process(self, event: object, key: _DisplayKey) -> list[ModelStreamPayload]:
        raw_event = cast(Any, event)
        event_type = getattr(raw_event, "type", "")
        payloads: list[ModelStreamPayload] = []
        if event_type in {
            "response.output_text.delta",
            "response.refusal.delta",
        }:
            if key not in self.started:
                self.started.add(key)
                self.active = key
                payloads.append(ModelTextStarted())
            payloads.append(ModelTextDelta(str(raw_event.delta)))
            return payloads
        if event_type == "response.reasoning_summary_text.delta":
            if key not in self.started:
                self.started.add(key)
                self.active = key
                payloads.append(ModelReasoningStarted("summary"))
            payloads.append(
                ModelReasoningDelta(
                    "summary",
                    int(getattr(raw_event, "summary_index", 0)),
                    str(raw_event.delta),
                )
            )
            return payloads
        if event_type == "response.output_item.done":
            raw = _model_dump(raw_event.item)
            if raw.get("type") == "reasoning":
                presentation = _reasoning_presentation(raw)
                if key not in self.started:
                    self.started.add(key)
                    payloads.append(ModelReasoningStarted(presentation.disclosure))
                payloads.append(ModelReasoningCompleted(presentation))
            else:
                text = _message_text(raw)
                if not text and key not in self.started:
                    return []
                if key not in self.started:
                    self.started.add(key)
                    payloads.append(ModelTextStarted())
                payloads.append(ModelTextCompleted(text))
            self.completed.add(key)
            if self.active == key:
                self.active = None
            return payloads
        return payloads


class OpenAIResponsesProvider(ModelClient):
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

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        params = self._request_params(request)
        params["stream"] = True
        final: object | None = None
        sequencer = ModelStreamSequencer()
        normalizer = _OpenAIStreamNormalizer()
        last_provider_sequence: int | None = None
        try:
            stream = await self.client.responses.create(**cast(Any, params))
            async for event in cast(Any, stream):
                event_type = getattr(event, "type", "")
                provider_sequence = getattr(event, "sequence_number", None)
                if isinstance(provider_sequence, int):
                    if (
                        last_provider_sequence is not None
                        and provider_sequence <= last_provider_sequence
                    ):
                        raise RuntimeError(
                            "OpenAI Responses stream sequence numbers are not "
                            "increasing"
                        )
                    last_provider_sequence = provider_sequence
                if event_type in {"response.completed", "response.incomplete"}:
                    final = event.response
                elif event_type == "response.failed":
                    raise RuntimeError(f"OpenAI response failed: {event.response}")
                else:
                    for payload in normalizer.feed(event):
                        yield sequencer.emit(payload)
        except BadRequestError as error:
            _raise_context_overflow(error)
            raise
        if final is None:
            raise RuntimeError("OpenAI Responses stream ended without a final response")
        output = self._response(final)
        for payload in normalizer.reconcile(final):
            yield sequencer.emit(payload)
        yield sequencer.emit(ModelOutputCompleted(output))

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
            "input": self._input(request.input),
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

    def _input(self, items: Iterable[ModelInputItem]) -> list[object]:
        result: list[object] = []
        for item in items:
            if isinstance(item, UserInput):
                result.append(_openai_user_input(item))
            elif isinstance(item, AssistantOutput):
                for block in item.content:
                    continuation = getattr(block, "continuation", None)
                    if (
                        continuation is not None
                        and continuation.binding == self.binding
                    ):
                        result.append(_openai_item(continuation))
                    elif isinstance(block, ModelTextBlock):
                        result.append({"role": "assistant", "content": block.text})
                    elif isinstance(block, ModelToolUseBlock):
                        result.append(
                            {
                                "type": "function_call",
                                "call_id": block.id,
                                "name": block.name,
                                "arguments": json.dumps(
                                    block.input,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                ),
                            }
                        )
            elif isinstance(item, ToolOutputs):
                for output in item.results:
                    result.append(
                        {
                            "type": "function_call_output",
                            "call_id": output.call_id,
                            "output": _openai_tool_output(output),
                        }
                    )
        return result

    def _response(self, response: object) -> ModelOutput:
        content: list[ModelTextBlock | ModelToolUseBlock | ModelReasoningBlock] = []
        for item in getattr(response, "output", ()):
            payload = _model_dump(item)
            item_type = payload.get("type")
            if item_type in {"reasoning", "function_call", "message"}:
                _validate_openai_payload(payload)
            continuation = ProviderContinuationState(
                self.binding, "working_context", payload
            )
            if item_type == "reasoning":
                presentation = _reasoning_presentation(payload)
                content.append(
                    ModelReasoningBlock(
                        str(uuid4()),
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
                text = _message_text(payload)
                if text:
                    content.append(ModelTextBlock(text, continuation))
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
                provider_reported=usage is not None,
            ),
        )


def _model_dump(item: object) -> JsonObject:
    dump = getattr(item, "model_dump", None)
    if not callable(dump):
        raise TypeError("Expected an OpenAI response output item")
    return to_json_object(dump(mode="json", exclude_none=False))


def _reasoning_presentation(payload: JsonObject) -> ReasoningPresentation:
    parts = tuple(
        str(part["text"])
        for part in cast(list[object], payload.get("summary", []))
        if isinstance(part, dict)
        and part.get("type") == "summary_text"
        and isinstance(part.get("text"), str)
        and part["text"]
    )
    return (
        ReasoningPresentation("summary", parts)
        if parts
        else ReasoningPresentation("hidden")
    )


def _message_text(payload: JsonObject) -> str:
    texts: list[str] = []
    for part in cast(list[object], payload.get("content", [])):
        if not isinstance(part, dict):
            continue
        if part.get("type") == "output_text" and isinstance(part.get("text"), str):
            texts.append(cast(str, part["text"]))
        elif part.get("type") == "refusal" and isinstance(part.get("refusal"), str):
            texts.append(cast(str, part["refusal"]))
    return "\n".join(texts)


def _openai_item(state: ProviderContinuationState) -> JsonObject:
    payload = state.payload
    _validate_openai_payload(payload)
    return to_json_object(payload)


def _openai_user_input(item: UserInput) -> dict[str, object]:
    if len(item.content) == 1 and isinstance(item.content[0], InputText):
        return {"role": "user", "content": item.content[0].text}
    content: list[dict[str, object]] = []
    for block in item.content:
        if isinstance(block, InputText):
            content.append({"type": "input_text", "text": block.text})
        elif isinstance(block, InputImage):
            content.append(
                {
                    "type": "input_image",
                    "image_url": f"data:{block.media_type};base64,{block.data}",
                    "detail": "auto",
                }
            )
        elif isinstance(block, InputDocument):
            document: dict[str, object] = {
                "type": "input_file",
                "file_data": f"data:{block.media_type};base64,{block.data}",
            }
            if block.name is not None:
                document["filename"] = block.name
            content.append(document)
    return {"role": "user", "content": content}


def _openai_tool_output(output: ToolOutput) -> str | list[dict[str, object]]:
    text_blocks = tuple(
        block for block in output.content if isinstance(block, ToolOutputText)
    )
    if len(text_blocks) == len(output.content):
        text = "\n".join(block.text for block in text_blocks)
        return f"Error: {text}" if output.is_error else text
    content: list[dict[str, object]] = []
    if output.is_error:
        content.append({"type": "input_text", "text": "Error:"})
    for block in output.content:
        if isinstance(block, ToolOutputText):
            content.append({"type": "input_text", "text": block.text})
        elif isinstance(block, ToolOutputImage):
            content.append(
                {
                    "type": "input_image",
                    "image_url": f"data:{block.media_type};base64,{block.data}",
                    "detail": "auto",
                }
            )
        elif isinstance(block, ToolOutputDocument):
            item: dict[str, object] = {
                "type": "input_file",
                "file_data": f"data:{block.media_type};base64,{block.data}",
            }
            if block.name is not None:
                item["filename"] = block.name
            content.append(item)
    return content


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
