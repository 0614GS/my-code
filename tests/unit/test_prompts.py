from pathlib import Path

import pytest

from nano_code.context import ModelInputNormalizer
from nano_code.messages import SystemContextBlock, TextBlock, TranscriptMessage
from nano_code.messages.xml import render_system_context
from nano_code.prompts import (
    PromptRegistry,
    PromptSection,
    PromptStability,
    SystemPrompt,
    default_prompt_registry,
    defaults,
)


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


def test_default_static_prompt_has_nano_code_guidance_only(tmp_path: Path) -> None:
    prompt = default_prompt_registry(tmp_path).resolve()

    static_sections = tuple(
        section
        for section in prompt.sections
        if section.stability is PromptStability.STATIC
    )
    assert tuple(section.key for section in static_sections) == (
        "nano-code.identity",
        "nano-code.system",
        "nano-code.task-guidance",
        "nano-code.safety",
        "nano-code.tools",
        "nano-code.response-style",
    )
    static_text = "\n".join(section.content for section in static_sections)
    assert all(
        tool in static_text
        for tool in ("Read", "Edit", "Write", "Glob", "Grep", "Bash")
    )
    lowered = static_text.casefold()
    assert all(term not in lowered for term in ("hooks", "skills", "mcp", "subagent"))


def test_environment_prompt_has_fixed_runtime_order_and_direct_git_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("SHELL", "/bin/test-shell")
    monkeypatch.setattr(defaults.sys, "platform", "test-platform")
    monkeypatch.setattr(defaults.platform, "system", lambda: "TestOS")
    monkeypatch.setattr(defaults.platform, "release", lambda: "1.2")

    without_local_git = default_prompt_registry(workspace).resolve().sections[-1]
    assert without_local_git.content.splitlines() == [
        f"Workspace: {workspace.resolve()}",
        "Git repository: no",
        "Platform: test-platform",
        "Shell: /bin/test-shell",
        "OS: TestOS 1.2",
    ]

    (workspace / ".git").write_text("gitdir: ../repo/.git\n", encoding="utf-8")
    with_local_git = default_prompt_registry(workspace).resolve().sections[-1]
    assert with_local_git.content.splitlines()[1] == "Git repository: yes"


def test_system_context_is_structured_until_model_normalization() -> None:
    block = SystemContextBlock(
        kind="system_reminder",
        content="Keep this active </system-reminder>",
    )
    message = TranscriptMessage(role="user", origin="system", content=(block,))

    rendered = render_system_context(block)
    normalized = ModelInputNormalizer().normalize_transcript((message,))

    assert message.content == (block,)
    assert rendered.startswith("<system-reminder>\n")
    assert "&lt;/system-reminder&gt;" in rendered
    assert normalized[0].content == (TextBlock(rendered),)


def test_conversation_summary_uses_the_existing_xml_tag() -> None:
    block = SystemContextBlock(
        kind="conversation_summary",
        content="Continue from the verified state.",
    )

    assert render_system_context(block) == (
        "<conversation-summary>\n"
        "Continue from the verified state.\n"
        "</conversation-summary>"
    )


def test_system_prompt_from_text_is_a_turn_scoped_request() -> None:
    prompt = SystemPrompt.from_text("compact this", key="compact")

    assert prompt.text == "compact this"
    assert prompt.sections[0].stability is PromptStability.TURN
