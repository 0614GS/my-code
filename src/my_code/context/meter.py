"""Single source of truth for request footprints and context projections."""

import json
import os
import tempfile
import warnings
from dataclasses import dataclass
from pathlib import Path

from my_code.context.tokenizer import NullTokenCounter, TokenCounter
from my_code.model.primitives import ContextFootprint, ProviderBinding, TokenUsage
from my_code.model.request import (
    AssistantOutput,
    InputDocument,
    InputImage,
    InputText,
    ModelReasoningBlock,
    ModelRequest,
    ModelTextBlock,
    ModelToolUseBlock,
    ToolOutputDocument,
    ToolOutputImage,
    ToolOutputs,
    ToolOutputText,
    UserInput,
)

IMAGE_TOKENS = 6_000
DOCUMENT_TOKENS = 20_000


@dataclass(frozen=True, slots=True)
class TokenEstimate:
    tokens: int
    source: str


class ContextMeter:
    """Serialize once, estimate temporarily, and anchor projections in usage."""

    def __init__(
        self,
        *,
        counter: TokenCounter | None = None,
        cache_path: Path | None = None,
    ) -> None:
        self.counter = counter or NullTokenCounter()
        self.cache_path = cache_path or Path.home() / ".my-code/.token-estimates.json"
        self._ratios = self._load_ratios()

    def footprint(self, request: ModelRequest) -> ContextFootprint:
        value, images, documents = _request_value(request)
        return ContextFootprint(_json(value), images, documents)

    def response_footprint(
        self, request: ModelRequest, response: AssistantOutput
    ) -> ContextFootprint:
        value, images, documents = _request_value(request, response=response)
        return ContextFootprint(_json(value), images, documents)

    def estimate(
        self, binding: ProviderBinding | None, footprint: ContextFootprint
    ) -> TokenEstimate:
        model = binding.model if binding is not None else "unknown"
        counted = self.counter.count(model, footprint.text)
        if counted is not None:
            if counted < 0:
                raise ValueError("TokenCounter returned a negative count")
            text_tokens = counted
            source = "token_counter"
        else:
            ratio = self._ratios.get(_key(binding)) if binding is not None else None
            if ratio is None:
                text_tokens = _ceil_ratio(len(footprint.text), 4)
                source = "four_chars_per_token"
            else:
                chars, tokens = ratio
                text_tokens = _ceil_ratio(len(footprint.text) * tokens, chars)
                source = "calibrated_ratio"
        return TokenEstimate(
            max(
                1,
                text_tokens
                + footprint.image_count * IMAGE_TOKENS
                + footprint.document_count * DOCUMENT_TOKENS,
            ),
            source,
        )

    def calibrate(
        self,
        binding: ProviderBinding | None,
        request_footprint: ContextFootprint,
        usage: TokenUsage,
    ) -> None:
        if (
            binding is None
            or request_footprint.has_media
            or not usage.provider_reported
            or usage.total_input_tokens < 1
        ):
            return
        key = _key(binding)
        if key in self._ratios:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError:
            return
        lock = self.cache_path.with_suffix(self.cache_path.suffix + ".lock")
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return
        except OSError:
            return
        os.close(descriptor)
        temporary: str | None = None
        try:
            current = self._read_cache(warn=False)
            if key in current:
                self._ratios = current
                return
            current[key] = (len(request_footprint.text), usage.total_input_tokens)
            fd, temporary = tempfile.mkstemp(
                prefix=f".{self.cache_path.name}.", dir=self.cache_path.parent
            )
            try:
                os.fchmod(fd, 0o600)
                payload = json.dumps(
                    {name: list(value) for name, value in sorted(current.items())},
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode()
                view = memoryview(payload)
                while view:
                    written = os.write(fd, view)
                    if written < 1:
                        raise OSError("token estimate cache write made no progress")
                    view = view[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temporary, self.cache_path)
            temporary = None
            self._ratios = current
        except OSError:
            return
        finally:
            if temporary is not None:
                try:
                    Path(temporary).unlink()
                except FileNotFoundError:
                    pass
            try:
                lock.unlink()
            except FileNotFoundError:
                pass

    def _load_ratios(self) -> dict[str, tuple[int, int]]:
        return self._read_cache(warn=True)

    def _read_cache(self, *, warn: bool) -> dict[str, tuple[int, int]]:
        if not self.cache_path.exists():
            return {}
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("root must be an object")
            parsed: dict[str, tuple[int, int]] = {}
            for key, value in raw.items():
                if (
                    not isinstance(key, str)
                    or not isinstance(value, list)
                    or len(value) != 2
                    or not all(isinstance(part, int) and part > 0 for part in value)
                ):
                    raise ValueError("invalid ratio entry")
                parsed[key] = (value[0], value[1])
            return parsed
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            if warn:
                warnings.warn(
                    f"Ignoring corrupt token estimate cache {self.cache_path}: {error}",
                    RuntimeWarning,
                    stacklevel=2,
                )
            return {}


def _request_value(
    request: ModelRequest, *, response: AssistantOutput | None = None
) -> tuple[object, int, int]:
    images = 0
    documents = 0

    def media(block: object) -> object:
        nonlocal images, documents
        if isinstance(block, (InputImage, ToolOutputImage)):
            images += 1
            return {
                "type": block.type,
                "media_type": block.media_type,
                "data": "<media>",
            }
        if isinstance(block, (InputDocument, ToolOutputDocument)):
            documents += 1
            return {
                "type": block.type,
                "media_type": block.media_type,
                "name": block.name,
                "data": "<media>",
            }
        if isinstance(block, InputText):
            return {"type": block.type, "text": block.text}
        if isinstance(block, ToolOutputText):
            return {"type": block.type, "text": block.text}
        raise TypeError(f"Unsupported media/text block: {type(block).__name__}")

    def assistant(block: object) -> object:
        continuation = getattr(block, "continuation", None)
        common = {
            "continuation": continuation.payload if continuation is not None else None
        }
        if isinstance(block, ModelTextBlock):
            return {"type": block.type, "text": block.text, **common}
        if isinstance(block, ModelToolUseBlock):
            return {
                "type": block.type,
                "id": block.id,
                "name": block.name,
                "input": block.input,
                **common,
            }
        if isinstance(block, ModelReasoningBlock):
            return {"type": block.type, "id": block.id, **common}
        raise TypeError(f"Unsupported assistant block: {type(block).__name__}")

    inputs: list[object] = []
    for item in request.input:
        if isinstance(item, UserInput):
            inputs.append(
                {"type": item.type, "content": [media(b) for b in item.content]}
            )
        elif isinstance(item, AssistantOutput):
            inputs.append(
                {"type": item.type, "content": [assistant(b) for b in item.content]}
            )
        elif isinstance(item, ToolOutputs):
            inputs.append(
                {
                    "type": item.type,
                    "results": [
                        {
                            "call_id": result.call_id,
                            "is_error": result.is_error,
                            "content": [media(b) for b in result.content],
                        }
                        for result in item.results
                    ],
                }
            )
    if response is not None:
        inputs.append(
            {
                "type": response.type,
                "content": [assistant(b) for b in response.content],
            }
        )
    value = {
        "system": [
            {"key": section.key, "content": section.content}
            for section in request.system_prompt.sections
        ],
        "input": inputs,
        "tools": [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            }
            for tool in request.tools
        ],
        "reasoning_mode": request.reasoning_mode,
    }
    return value, images, documents


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _key(binding: ProviderBinding) -> str:
    return f"{binding.provider_id}:{binding.model}"


def _ceil_ratio(numerator: int, denominator: int) -> int:
    return (numerator + denominator - 1) // denominator


__all__ = ["ContextMeter", "DOCUMENT_TOKENS", "IMAGE_TOKENS", "TokenEstimate"]
