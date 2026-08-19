"""Deterministic Unicode-aware request token estimation."""

import json
import unicodedata

from my_code.model.request import (
    AssistantOutput,
    InputText,
    ModelReasoningBlock,
    ModelRequest,
    ModelTextBlock,
    ModelToolUseBlock,
    ToolOutputs,
    ToolOutputText,
    UserInput,
)


class UnicodeTokenEstimator:
    """Small conservative tokenizer used when providers expose no count API."""

    def count_text(self, text: str) -> int:
        tokens = 0
        ascii_run = bytearray()

        def flush() -> None:
            nonlocal tokens
            if ascii_run:
                tokens += max(1, (len(ascii_run) + 3) // 4)
                ascii_run.clear()

        for char in text:
            codepoint = ord(char)
            category = unicodedata.category(char)
            if char == "\n":
                flush()
                tokens += 1
            elif char.isspace():
                flush()
                tokens += 1
            elif _is_cjk(codepoint):
                flush()
                tokens += 1
            elif codepoint < 128 and (char.isalnum() or char == "_"):
                ascii_run.extend(char.encode())
            elif category.startswith(("L", "N", "M")):
                flush()
                tokens += max(1, (len(char.encode("utf-8")) + 2) // 3)
            else:
                flush()
                # JSON/code punctuation and emoji are never hidden in a /4 average.
                tokens += max(1, (len(char.encode("utf-8")) + 1) // 2)
        flush()
        return tokens

    def count_request(self, request: ModelRequest) -> int:
        count = self.count_text(request.system_prompt.text) + 4
        for tool in request.tools:
            count += 8
            count += self.count_text(tool.name) + self.count_text(tool.description)
            count += self.count_text(_json(tool.input_schema))
        for item in request.input:
            count += 5
            count += 1 if isinstance(item, AssistantOutput) else 0
            if isinstance(item, ToolOutputs):
                for output in item.results:
                    count += 3 + sum(
                        self.count_text(
                            block.text
                            if isinstance(block, ToolOutputText)
                            else block.data
                        )
                        for block in output.content
                    )
                continue
            if isinstance(item, UserInput):
                for block in item.content:
                    count += 3
                    count += self.count_text(
                        block.text if isinstance(block, InputText) else block.data
                    )
                continue
            for block in item.content:
                count += 3
                continuation = getattr(block, "continuation", None)
                if continuation is not None:
                    count += self.count_text(_json(continuation.payload))
                elif isinstance(block, ModelTextBlock):
                    count += self.count_text(block.text)
                elif isinstance(block, ModelToolUseBlock):
                    count += self.count_text(block.name) + self.count_text(
                        _json(block.input)
                    )
                elif isinstance(block, ModelReasoningBlock):
                    # Provider-private reasoning without replay state is not input.
                    continue
        # Provider framing differs; keep an explicit ten-percent safety margin.
        return max(1, (count * 11 + 9) // 10)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _is_cjk(codepoint: int) -> bool:
    return (
        0x3400 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x3040 <= codepoint <= 0x30FF
        or 0xAC00 <= codepoint <= 0xD7AF
        or 0x20000 <= codepoint <= 0x323AF
    )


__all__ = ["UnicodeTokenEstimator"]
