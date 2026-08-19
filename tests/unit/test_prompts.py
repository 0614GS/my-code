from pathlib import Path

import pytest

import my_code.prompts.system as system
from my_code.context.documents import ContextInstruction
from my_code.context.normalization import ModelInputNormalizer
from my_code.context.xml import render_context_instruction, wrap_xml
from my_code.conversation.models import ConversationSummaryMessage
from my_code.model.request import InputText, PromptStability, SystemPrompt, UserInput
from my_code.prompts.models import PromptSection
from my_code.prompts.registry import PromptRegistry
from my_code.prompts.system import build_system_prompt_registry


def test_registry_caches_stable_sections_and_recomputes_request_sections() -> None:
    calls = {"static": 0, "session": 0, "request": 0}

    def resolve(key: str) -> str:
        calls[key] += 1
        return f"{key}-{calls[key]}"

    registry = PromptRegistry(
        (
            PromptSection("static", PromptStability.STATIC, lambda: resolve("static")),
            PromptSection(
                "session", PromptStability.SESSION, lambda: resolve("session")
            ),
            PromptSection(
                "request", PromptStability.REQUEST, lambda: resolve("request")
            ),
        )
    )

    assert calls == {"static": 1, "session": 0, "request": 0}
    assert tuple(item.key for item in registry.runtime_snapshot) == ("static",)

    session_cache = {}
    first = registry.resolve(session_cache=session_cache)
    second = registry.resolve(session_cache=session_cache)

    assert calls == {"static": 1, "session": 1, "request": 2}
    assert first.sections[:2] == second.sections[:2]
    assert first.sections[2] != second.sections[2]

    next_session = registry.resolve(session_cache={})
    assert calls == {"static": 1, "session": 2, "request": 3}
    assert next_session.sections[0] == first.sections[0]
    assert next_session.sections[1] != first.sections[1]


def test_registry_rejects_unstable_prefix_order() -> None:
    with pytest.raises(ValueError, match="ordered static, session, then request"):
        PromptRegistry(
            (
                PromptSection("request", PromptStability.REQUEST, lambda: "request"),
                PromptSection("static", PromptStability.STATIC, lambda: "static"),
            )
        )


def test_default_prompt_keeps_workspace_out_of_static_prefix(tmp_path: Path) -> None:
    prompt = build_system_prompt_registry(tmp_path).resolve()

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


def test_default_static_prompt_has_my_code_guidance_only(tmp_path: Path) -> None:
    prompt = build_system_prompt_registry(tmp_path).resolve()

    static_sections = tuple(
        section
        for section in prompt.sections
        if section.stability is PromptStability.STATIC
    )
    assert tuple(section.key for section in static_sections) == (
        "my-code.identity",
        "my-code.system",
        "my-code.task-guidance",
        "my-code.safety",
        "my-code.tools",
        "my-code.response-style",
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
    monkeypatch.setattr(system.sys, "platform", "test-platform")
    monkeypatch.setattr(system.platform, "system", lambda: "TestOS")
    monkeypatch.setattr(system.platform, "release", lambda: "1.2")

    without_local_git = build_system_prompt_registry(workspace).resolve().sections[-1]
    assert without_local_git.content.splitlines() == [
        f"Workspace: {workspace.resolve()}",
        "Git repository: no",
        "Platform: test-platform",
        "Shell: /bin/test-shell",
        "OS: TestOS 1.2",
    ]

    (workspace / ".git").write_text("gitdir: ../repo/.git\n", encoding="utf-8")
    with_local_git = build_system_prompt_registry(workspace).resolve().sections[-1]
    assert with_local_git.content.splitlines()[1] == "Git repository: yes"


def test_system_context_is_structured_until_model_normalization() -> None:
    block = ContextInstruction(content="Keep this active </system-reminder>")

    rendered = render_context_instruction(block)

    assert rendered.startswith("<system-reminder>\n")
    assert "&lt;/system-reminder&gt;" in rendered


def test_conversation_summary_uses_the_existing_xml_tag() -> None:
    summary = ConversationSummaryMessage("Continue from the verified state.")
    normalized = ModelInputNormalizer().normalize_transcript((summary,))

    assert isinstance(normalized[0], UserInput)
    assert normalized[0].content == (
        InputText(
            "<conversation-summary>\n"
            "Continue from the verified state.\n"
            "</conversation-summary>"
        ),
    )
    assert wrap_xml("conversation-summary", summary.content).startswith(
        "<conversation-summary>"
    )


def test_system_prompt_from_text_is_a_turn_scoped_request() -> None:
    prompt = SystemPrompt.from_text("compact this", key="compact")

    assert prompt.text == "compact this"
    assert prompt.sections[0].stability is PromptStability.REQUEST
