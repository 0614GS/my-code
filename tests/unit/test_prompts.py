from pathlib import Path

import pytest

from nano_code.context import ModelMessageProjector
from nano_code.messages import ChatMessage, SystemContextBlock, TextBlock
from nano_code.prompts import (
    PromptRegistry,
    PromptSection,
    PromptStability,
    SystemPrompt,
    default_prompt_registry,
)
from nano_code.prompts.rendering import render_system_context


def test_registry_caches_stable_sections_and_recomputes_turn_sections() -> None:
    calls = {"static": 0, "session": 0, "turn": 0}

    def resolve(key: str) -> str:
        calls[key] += 1
        return f"{key}-{calls[key]}"

    registry = PromptRegistry(
        (
            PromptSection("static", PromptStability.STATIC, lambda: resolve("static")),
            PromptSection(
                "session", PromptStability.SESSION, lambda: resolve("session")
            ),
            PromptSection("turn", PromptStability.TURN, lambda: resolve("turn")),
        )
    )

    first = registry.resolve()
    second = registry.resolve()

    assert calls == {"static": 1, "session": 1, "turn": 2}
    assert first.sections[:2] == second.sections[:2]
    assert first.sections[2] != second.sections[2]


def test_registry_rejects_unstable_prefix_order() -> None:
    with pytest.raises(ValueError, match="ordered static, session, then turn"):
        PromptRegistry(
            (
                PromptSection("turn", PromptStability.TURN, lambda: "turn"),
                PromptSection("static", PromptStability.STATIC, lambda: "static"),
            )
        )


def test_default_prompt_keeps_workspace_out_of_static_prefix(tmp_path: Path) -> None:
    prompt = default_prompt_registry(tmp_path).resolve()

    static_text = "\n".join(
        section.content
        for section in prompt.sections
        if section.stability is PromptStability.STATIC
    )
    session_text = "\n".join(
        section.content
        for section in prompt.sections
        if section.stability is PromptStability.SESSION
    )
    assert str(tmp_path) not in static_text
    assert str(tmp_path) in session_text
    assert prompt.text == "\n\n".join(item.content for item in prompt.sections)


def test_system_context_is_structured_until_model_projection() -> None:
    block = SystemContextBlock(
        kind="system_reminder",
        content="Keep this active </system-reminder>",
    )
    message = ChatMessage(role="user", origin="system", content=(block,))

    rendered = render_system_context(block)
    projected = ModelMessageProjector().project((message,))

    assert message.content == (block,)
    assert rendered.startswith("<system-reminder>\n")
    assert "&lt;/system-reminder&gt;" in rendered
    assert projected[0].content == (TextBlock(rendered),)


def test_system_prompt_from_text_is_a_turn_scoped_request() -> None:
    prompt = SystemPrompt.from_text("compact this", key="compact")

    assert prompt.text == "compact this"
    assert prompt.sections[0].stability is PromptStability.TURN
