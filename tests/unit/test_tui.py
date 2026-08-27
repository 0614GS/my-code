import asyncio
from collections.abc import AsyncIterator
from io import StringIO

import pytest
from prompt_toolkit.data_structures import Size
from prompt_toolkit.formatted_text import fragment_list_to_text, to_formatted_text
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.layout import Window
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.color_depth import ColorDepth
from rich.console import Console
from rich.padding import Padding

from my_code.auth.credentials import CredentialSource
from my_code.chat.events import (
    TextCompleted,
    TextDelta,
    TextStarted,
    TurnEvent,
    TurnSucceeded,
)
from my_code.chat.history import HistoryText
from my_code.chat.permissions import PermissionRequest
from my_code.chat.status import ContextStatus, RuntimeStatus
from my_code.chat.views import CapabilitiesView, SessionView, SubagentTaskView
from my_code.config.providers import ProviderProtocol
from my_code.permissions.models import PermissionConfirmation
from my_code.providers.manager import ProviderUpdate, ProviderView
from my_code.tools.presentation import ToolUsePresentation
from my_code.tui.app import MyCodeApp
from my_code.tui.commands import SlashCommandRegistry
from my_code.tui.dimensions import SURFACE_VERTICAL_PADDING
from my_code.tui.presentation import format_context_usage, render_context_status
from my_code.tui.provider_screen import ProviderForm
from my_code.tui.terminal import NativeCursorVt100Output, terminal_color_depth
from my_code.tui.theme import TerminalPalette, TuiTheme
from my_code.tui.widgets import user_message


class FakeRuntime:
    def __init__(
        self,
        *,
        history: tuple[HistoryText, ...] = (),
        credential_source: CredentialSource = CredentialSource.STORED,
    ) -> None:
        self.prompts: list[str] = []
        self.history = history
        self.permission_handler = None
        self.submitted = asyncio.Event()
        self.provider_updates: list[ProviderUpdate] = []
        self.removed_credentials: list[str] = []
        self.has_stored_key = True
        self.credential_source = credential_source
        self.agents: tuple[SubagentTaskView, ...] = ()

    async def initialize(self) -> SessionView:
        return SessionView(self.status(), self.history)

    def set_permission_handler(self, handler):
        self.permission_handler = handler

    async def stream(self, prompt: str) -> AsyncIterator[TurnEvent]:
        self.prompts.append(prompt)
        self.submitted.set()
        yield TextStarted()
        yield TextDelta("**model response**")
        yield TextCompleted("**model response**")
        yield TurnSucceeded("**model response**", 1, 10, 2)

    async def stream_background_notifications(self) -> AsyncIterator[TurnEvent]:
        while True:
            await asyncio.sleep(3600)
            yield TextDelta("")

    def status(self) -> RuntimeStatus:
        return RuntimeStatus(
            session_id="session-id",
            cwd="/workspace",
            provider_id="anthropic",
            base_url=None,
            model="test-model",
            permission_mode="default",
            credential_source=self.credential_source.value,
            context_entry_count=len(self.prompts) * 2,
            conversation_entry_count=len(self.prompts) * 2,
            todos=(),
            tool_count=5,
            skill_count=1,
        )

    def context_status(self) -> ContextStatus:
        return ContextStatus(
            estimated_input_tokens=100,
            reserved_output_tokens=20,
            estimated_total_tokens=120,
            message_chars=300,
            system_chars=100,
            tool_schema_chars=200,
            message_limit_chars=1000,
            context_entry_count=0,
            conversation_entry_count=0,
            replacement_count=1,
            compact_count=0,
            input_limit_tokens=200_000,
            compact_trigger_tokens=180_000,
        )

    def capabilities(self) -> CapabilitiesView:
        return CapabilitiesView((), (), (), ())

    def subagent_tasks(self):
        return self.agents

    async def stream_subagent_activity(self):
        while True:
            await asyncio.sleep(3600)
            yield ()

    def providers(self) -> tuple[ProviderView, ...]:
        return (
            ProviderView(
                "anthropic",
                ProviderProtocol.ANTHROPIC_MESSAGES,
                "test-model",
                None,
                True,
                self.has_stored_key,
                self.credential_source,
            ),
        )

    async def configure_provider(self, update: ProviderUpdate) -> RuntimeStatus:
        self.provider_updates.append(update)
        return self.status()

    async def remove_provider_credential(self, provider_id: str) -> RuntimeStatus:
        self.removed_credentials.append(provider_id)
        self.has_stored_key = False
        if self.credential_source is CredentialSource.STORED:
            self.credential_source = CredentialSource.NONE
        return self.status()

    async def suggest_paths(self, query: str):
        del query
        return ()


@pytest.mark.asyncio
async def test_inline_host_submits_multiline_and_keeps_normal_scrollback() -> None:
    runtime = FakeRuntime()
    stream = StringIO()
    with create_pipe_input() as pipe:
        app = MyCodeApp(
            runtime,  # type: ignore[arg-type]
            input=pipe,
            output=DummyOutput(),
            console=Console(file=stream, width=100, force_terminal=False),
        )
        running = asyncio.create_task(app.run_async())
        await asyncio.sleep(0.05)
        pipe.send_text("first\x1b\rsecond\r")
        await asyncio.wait_for(runtime.submitted.wait(), 1)
        assert runtime.prompts == ["first\nsecond"]
        await asyncio.sleep(0.05)
        pipe.send_bytes(b"\x04")
        await running

    output = stream.getvalue()
    assert "model response" in output
    assert "Done · 1 steps" in output
    assert "\x1b[?1049" not in output


@pytest.mark.asyncio
async def test_ctrl_d_requires_an_empty_composer() -> None:
    runtime = FakeRuntime()
    with create_pipe_input() as pipe:
        app = MyCodeApp(
            runtime,  # type: ignore[arg-type]
            input=pipe,
            output=DummyOutput(),
            console=Console(file=StringIO(), force_terminal=False),
        )
        running = asyncio.create_task(app.run_async())
        await asyncio.sleep(0.05)
        pipe.send_text("draft")
        pipe.send_bytes(b"\x04")
        await asyncio.sleep(0.05)
        assert not running.done()
        pipe.send_bytes(b"\x03\x04")
        await running


@pytest.mark.asyncio
async def test_startup_renders_safe_restored_history() -> None:
    stream = StringIO()
    runtime = FakeRuntime(
        history=(
            HistoryText("user", "old prompt"),
            HistoryText("assistant", "old answer"),
        )
    )
    with create_pipe_input() as pipe:
        app = MyCodeApp(
            runtime,  # type: ignore[arg-type]
            input=pipe,
            output=DummyOutput(),
            console=Console(file=stream, force_terminal=False),
        )
        running = asyncio.create_task(app.run_async())
        await asyncio.sleep(0.05)
        pipe.send_bytes(b"\x04")
        await running

    assert "old prompt" in stream.getvalue()
    assert "old answer" in stream.getvalue()


def test_slash_opens_command_suggestions_while_typing() -> None:
    app = MyCodeApp(
        FakeRuntime(),  # type: ignore[arg-type]
        output=DummyOutput(),
        console=Console(file=StringIO(), force_terminal=False),
    )
    app.buffer.text = "/"

    assert app._slash_active()
    assert app._slash_menu.selected == 0
    assert {item.name for item in app._slash_menu.matches} >= {
        "help",
        "provider",
        "resume",
    }
    menu = app._slash_menu_text()
    assert menu[0][0] == "class:selected"


def test_refresh_status_isolates_context_failure() -> None:
    runtime = FakeRuntime()
    app = MyCodeApp(runtime)  # type: ignore[arg-type]
    previous = runtime.context_status()
    app._context_status = previous

    def fail_context() -> ContextStatus:
        raise RuntimeError("unresolved tool use")

    runtime.context_status = fail_context  # type: ignore[method-assign]

    app._refresh_status()

    assert app._status is not None
    assert app._context_status is previous
    assert app._status_warning == "context: RuntimeError"


def test_auth_slash_command_is_removed_from_help_completion_and_dispatch() -> None:
    registry = SlashCommandRegistry.default()
    status = FakeRuntime().status()

    assert "/auth" not in registry.render_help()
    assert registry.matching("/auth") == ()
    outcome = registry.dispatch("/auth", status=status)
    assert outcome is not None
    assert outcome.message.startswith("Unknown command")


def test_tab_only_inserts_the_selected_slash_command() -> None:
    runtime = FakeRuntime()
    app = MyCodeApp(
        runtime,  # type: ignore[arg-type]
        output=DummyOutput(),
        console=Console(file=StringIO(), force_terminal=False),
    )
    app.buffer.text = "/sta"

    app._accept_slash(execute=False)

    assert app.buffer.text == "/status "
    assert runtime.prompts == []
    assert not app._slash_active()


@pytest.mark.asyncio
async def test_slash_menu_is_started_by_real_input_without_tab() -> None:
    with create_pipe_input() as pipe:
        app = MyCodeApp(
            FakeRuntime(),  # type: ignore[arg-type]
            input=pipe,
            output=DummyOutput(),
            console=Console(file=StringIO(), force_terminal=False),
        )
        running = asyncio.create_task(app.run_async())
        await asyncio.sleep(0.05)
        pipe.send_text("/")
        for _ in range(20):
            if app._slash_active():
                break
            await asyncio.sleep(0.01)

        assert app._slash_active()
        assert len(app._slash_menu.matches) > 1
        pipe.send_bytes(b"\x03\x04")
        await running


def test_composer_preserves_the_native_terminal_cursor() -> None:
    app = MyCodeApp(
        FakeRuntime(),  # type: ignore[arg-type]
        output=DummyOutput(),
        console=Console(file=StringIO(), force_terminal=False),
    )

    shape = app.application.cursor.get_cursor_shape(app.application)
    assert shape.value == "_NEVER_CHANGE"


def test_vt_output_shows_cursor_without_disabling_terminal_blink() -> None:
    stream = StringIO()
    output = NativeCursorVt100Output(stream, lambda: Size(rows=24, columns=80))

    output.hide_cursor()
    output.show_cursor()
    output.flush()

    rendered = stream.getvalue()
    assert "\x1b[?12h" in rendered
    assert "\x1b[?25h" in rendered
    assert "\x1b[?12l" not in rendered


@pytest.mark.asyncio
async def test_enter_executes_the_default_selected_slash_command() -> None:
    runtime = FakeRuntime()
    stream = StringIO()
    with create_pipe_input() as pipe:
        app = MyCodeApp(
            runtime,  # type: ignore[arg-type]
            input=pipe,
            output=DummyOutput(),
            console=Console(file=stream, force_terminal=False),
        )
        running = asyncio.create_task(app.run_async())
        await asyncio.sleep(0.05)
        pipe.send_text("/sta")
        for _ in range(20):
            if app._slash_menu.current is not None:
                break
            await asyncio.sleep(0.01)
        assert app._slash_menu.current is not None
        assert app._slash_menu.current.name == "status"
        pipe.send_text("\r")
        await asyncio.sleep(0.05)
        pipe.send_bytes(b"\x04")
        await running

    assert runtime.prompts == []
    assert "Session:" in stream.getvalue()


def test_status_render_uses_cached_context_during_an_in_flight_tool_call() -> None:
    class InFlightRuntime(FakeRuntime):
        def context_status(self) -> ContextStatus:
            raise ValueError("Unresolved tool use in model input: call_01")

    runtime = InFlightRuntime()
    app = MyCodeApp(
        runtime,  # type: ignore[arg-type]
        output=DummyOutput(),
        console=Console(file=StringIO(), force_terminal=False),
    )
    app._status = runtime.status()
    app._context_status = FakeRuntime().context_status()

    assert app._status_text().endswith("0.1k / 200k")


def test_slash_menu_is_below_composer_and_uses_terminal_background() -> None:
    app = MyCodeApp(
        FakeRuntime(),  # type: ignore[arg-type]
        output=DummyOutput(),
        console=Console(file=StringIO(), force_terminal=False),
    )

    composer_index = next(
        index
        for index, child in enumerate(app.body.children)
        if getattr(child, "content", None) is app.input_control
    )
    menu_index = app.body.children.index(app.slash_menu)
    assert menu_index > composer_index

    style = app.application.style
    assert style is not None
    normal = style.get_attrs_for_style_str("class:completion-menu.meta.completion")
    selected = style.get_attrs_for_style_str("class:completion-menu.completion.current")
    assert normal.bgcolor == "default"
    assert selected.bgcolor == "default"
    assert selected.reverse is False
    menu_window = app.completions_menu.content
    assert isinstance(menu_window, Window)
    assert menu_window.right_margins == []


def test_user_message_matches_composer_vertical_padding() -> None:
    theme = TuiTheme(TerminalPalette((48, 10, 36)))
    message = user_message("hello", theme)

    assert isinstance(message, Padding)
    assert message.expand is True
    assert message.style == "on #49273e"
    assert message.top == SURFACE_VERTICAL_PADDING
    assert message.bottom == SURFACE_VERTICAL_PADDING


def test_streaming_assistant_text_renders_markdown_before_completion() -> None:
    app = MyCodeApp(
        FakeRuntime(),  # type: ignore[arg-type]
        output=DummyOutput(),
        console=Console(file=StringIO(), width=80, force_terminal=False),
    )
    app._stream_text = "**partial response**\n\n- first item"

    rendered = to_formatted_text(app._dynamic_text())
    text = fragment_list_to_text(rendered)

    assert "**" not in text
    assert "partial response" in text
    assert "• first item" in text
    assert any("bold" in style for style, *_ in rendered)


def test_theme_adapts_user_surface_to_light_and_dark_terminals() -> None:
    dark = TerminalPalette((0, 0, 0))
    light = TerminalPalette((255, 255, 255))

    assert dark.surface == "#1f1f1f"
    assert light.surface == "#f5f5f5"
    assert dark.accent == "#46a6e8"
    assert light.accent == "#005f87"


def test_windows_terminal_forces_true_color(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WT_SESSION", "test-session")

    assert terminal_color_depth(DummyOutput()) is ColorDepth.DEPTH_24_BIT


def test_slash_navigation_does_not_wrap_in_the_wrong_direction() -> None:
    app = MyCodeApp(
        FakeRuntime(),  # type: ignore[arg-type]
        output=DummyOutput(),
        console=Console(file=StringIO(), force_terminal=False),
    )
    app.buffer.text = "/"

    app._move_slash(-1)
    assert app._slash_menu.selected == 0
    app._move_slash(1)
    assert app._slash_menu.selected == 1


@pytest.mark.asyncio
async def test_real_arrow_sequences_move_slash_selection_in_screen_direction() -> None:
    with create_pipe_input() as pipe:
        app = MyCodeApp(
            FakeRuntime(),  # type: ignore[arg-type]
            input=pipe,
            output=DummyOutput(),
            console=Console(file=StringIO(), force_terminal=False),
        )
        running = asyncio.create_task(app.run_async())
        await asyncio.sleep(0.05)
        pipe.send_text("/")
        for _ in range(20):
            if app._slash_active():
                break
            await asyncio.sleep(0.01)

        assert app._slash_menu.selected == 0
        pipe.send_bytes(b"\x1b[B")
        await asyncio.sleep(0.05)
        assert app._slash_menu.selected == 1
        pipe.send_bytes(b"\x1b[A")
        await asyncio.sleep(0.05)
        assert app._slash_menu.selected == 0

        pipe.send_bytes(b"\x03\x04")
        await running


@pytest.mark.asyncio
async def test_provider_picker_uses_enter_for_primary_actions() -> None:
    runtime = FakeRuntime()
    app = MyCodeApp(
        runtime,  # type: ignore[arg-type]
        output=DummyOutput(),
        console=Console(file=StringIO(), force_terminal=False),
    )
    app.buffer.text = "draft"
    app._open_provider()

    assert app._panel == "provider_select"
    await app._panel_enter()
    assert app._panel == "provider_actions"
    assert "Use this provider" in fragment_list_to_text(
        to_formatted_text(app._panel_text())
    )

    await app._panel_enter()
    assert [update.id for update in runtime.provider_updates] == ["anthropic"]
    assert app._panel is None
    assert app.buffer.text == "draft"


@pytest.mark.asyncio
async def test_provider_credential_removal_requires_confirmation_and_can_cancel() -> (
    None
):
    runtime = FakeRuntime()
    app = MyCodeApp(
        runtime,  # type: ignore[arg-type]
        output=DummyOutput(),
        console=Console(file=StringIO(), force_terminal=False),
    )
    app._open_provider()
    await app._panel_enter()
    app._panel_index = 2
    await app._panel_enter()

    assert app._panel == "provider_remove_credential"
    assert app._panel_index == 1
    await app._panel_enter()

    assert app._panel == "provider_actions"
    assert runtime.removed_credentials == []
    assert runtime.has_stored_key is True


@pytest.mark.asyncio
async def test_provider_credential_removal_reports_environment_override() -> None:
    runtime = FakeRuntime(credential_source=CredentialSource.ENVIRONMENT)
    output = StringIO()
    app = MyCodeApp(
        runtime,  # type: ignore[arg-type]
        output=DummyOutput(),
        console=Console(file=output, force_terminal=False),
    )
    app.buffer.text = "draft"
    app._open_provider()
    await app._panel_enter()
    actions = fragment_list_to_text(to_formatted_text(app._panel_text()))
    assert "environment" in actions
    assert "Remove saved API key" in actions
    app._panel_index = 2
    await app._panel_enter()
    app._panel_index = 0
    await app._panel_enter()

    assert runtime.removed_credentials == ["anthropic"]
    assert app._panel is None
    assert app.buffer.text == "draft"
    assert "environment API key remains active" in output.getvalue()


def test_provider_panel_reports_not_configured_without_remove_action() -> None:
    runtime = FakeRuntime(credential_source=CredentialSource.NONE)
    runtime.has_stored_key = False
    app = MyCodeApp(
        runtime,  # type: ignore[arg-type]
        output=DummyOutput(),
        console=Console(file=StringIO(), force_terminal=False),
    )
    app._open_provider()
    app._provider_selected_index = 0
    app._panel = "provider_actions"

    text = fragment_list_to_text(to_formatted_text(app._panel_text()))

    assert "not configured" in text
    assert "Remove saved API key" not in text


@pytest.mark.asyncio
async def test_provider_configuration_has_core_review_and_optional_advanced_steps() -> (
    None
):
    app = MyCodeApp(
        FakeRuntime(),  # type: ignore[arg-type]
        output=DummyOutput(),
        console=Console(file=StringIO(), force_terminal=False),
    )
    app._provider_selected_index = -1
    app._start_provider_form(ProviderForm())
    core_values = (
        "gateway",
        "openai-responses",
        "https://example.test/v1",
        "model-x",
        "secret",
    )
    for value in core_values:
        app.buffer.text = value
        await app._panel_enter()

    assert app._panel == "provider_review"
    assert "Advanced settings" in fragment_list_to_text(
        to_formatted_text(app._panel_text())
    )
    app._panel_index = 2
    await app._panel_enter()
    assert app._panel == "provider_form"
    assert "1/7" in fragment_list_to_text(to_formatted_text(app._panel_text()))


def test_new_slash_commands_have_strict_subcommands() -> None:
    registry = SlashCommandRegistry.default()
    status = FakeRuntime().status()

    assert registry.dispatch("/usage", status=status).show_usage  # type: ignore[union-attr]
    assert registry.dispatch("/tools", status=status).show_tools  # type: ignore[union-attr]
    skills = registry.dispatch("/skills reload", status=status)
    mcp = registry.dispatch("/mcp refresh local", status=status)
    assert skills is not None and skills.skill_operation == "reload"
    assert mcp is not None and mcp.mcp_operation == ("refresh", "local")
    assert registry.dispatch("/tasks", status=status).show_tasks  # type: ignore[union-attr]
    invalid = registry.dispatch("/mcp refresh", status=status)
    assert invalid is not None and invalid.message.startswith("Usage:")


def test_context_usage_remains_compact() -> None:
    status = FakeRuntime().context_status()
    assert format_context_usage(status) == "0.1k / 200k"
    assert render_context_status(status).splitlines() == [
        "Context: 0.1k / 200k",
        "Measured by: local estimate",
        "Compact at: 180k (auto)",
        "Compactions: 1 micro · 0 full",
    ]


@pytest.mark.asyncio
async def test_permission_panel_returns_feedback_and_restores_draft() -> None:
    runtime = FakeRuntime()
    app = MyCodeApp(
        runtime,  # type: ignore[arg-type]
        output=DummyOutput(),
        console=Console(file=StringIO(), force_terminal=False),
    )
    app.buffer.text = "unfinished draft"
    pending = asyncio.create_task(
        app._ask_permission(
            PermissionRequest(
                "Write",
                {"path": "a.txt"},
                "Allow this write?",
                ToolUsePresentation("Write", "a.txt", "Writing a.txt"),
            )
        )
    )
    await asyncio.sleep(0)
    app._choose_permission("feedback")
    app.buffer.text = "Use another file."
    await app._panel_enter()

    assert await pending == PermissionConfirmation(False, "Use another file.")
    assert app.buffer.text == "unfinished draft"


def subagent_view(task_id: str, description: str) -> SubagentTaskView:
    return SubagentTaskView(
        task_id,
        f"run-{task_id}",
        "general",
        description,
        False,
        "running",
        "2026-08-27T00:00:00+00:00",
        "2026-08-27T00:00:00+00:00",
        None,
        0,
        0,
        (HistoryText("user", f"prompt-{task_id}"),),
        (),
    )


@pytest.mark.asyncio
async def test_agent_view_selection_uses_stable_task_id() -> None:
    runtime = FakeRuntime()
    first = subagent_view("first", "First agent")
    second = subagent_view("second", "Second agent")
    runtime.agents = (first, second)
    app = MyCodeApp(
        runtime,  # type: ignore[arg-type]
        output=DummyOutput(),
        console=Console(file=StringIO(), force_terminal=False),
    )

    app._open_agents()
    app._panel_index = 1
    await app._panel_enter()
    runtime.agents = (second, first)
    app._agents = runtime.agents

    assert app._agent_task_id == "first"
    assert "First agent" in app._agent_panel_text()
    assert "Second agent" not in app._agent_panel_text()


@pytest.mark.asyncio
async def test_bash_permission_defaults_to_no_and_has_only_three_choices() -> None:
    runtime = FakeRuntime()
    app = MyCodeApp(
        runtime,  # type: ignore[arg-type]
        output=DummyOutput(),
        console=Console(file=StringIO(), force_terminal=False),
    )
    request = PermissionRequest(
        "Bash",
        {"command": "git push origin main"},
        "Allow Bash?",
        ToolUsePresentation("Bash", "git push origin main", "Running Bash"),
    )
    pending = asyncio.create_task(app._ask_permission(request))
    await asyncio.sleep(0)

    assert app._panel_index == 2
    assert app._permission_options() == ("allow", "second", "third")
    app._choose_permission("third")
    assert await pending == PermissionConfirmation(False)


@pytest.mark.asyncio
async def test_permission_modal_restores_selected_agent_and_blocks_f6() -> None:
    runtime = FakeRuntime()
    runtime.agents = (subagent_view("first", "First agent"),)
    app = MyCodeApp(
        runtime,  # type: ignore[arg-type]
        output=DummyOutput(),
        console=Console(file=StringIO(), force_terminal=False),
    )
    app._cycle_agent_view()
    pending = asyncio.create_task(
        app._ask_permission(
            PermissionRequest(
                "Write",
                {"path": "a.txt"},
                "Allow write?",
                ToolUsePresentation("Write", "a.txt", "Writing a.txt"),
            )
        )
    )
    await asyncio.sleep(0)

    app._cycle_agent_view()
    assert app._panel == "permission"
    app._choose_permission("deny")
    assert await pending == PermissionConfirmation(False)
    assert app._panel == "agents"
    assert app._agent_task_id == "first"


def test_provider_form_preserves_all_fields_and_password() -> None:
    update = ProviderForm(
        provider_id="gateway",
        protocol=ProviderProtocol.OPENAI_RESPONSES.value,
        base_url="https://gateway.example/v1",
        model="model-x",
        context_window="200000",
        max_input="180000",
        max_output="16000",
        compact_trigger="150000",
        reasoning_enabled="yes",
        reasoning_effort="high",
        reasoning_context="all_turns",
        api_key="secret-key",
    ).build_update()

    assert update.id == "gateway"
    assert update.protocol is ProviderProtocol.OPENAI_RESPONSES
    assert update.api_key == "secret-key"
    assert update.limits.context_window_tokens == 200_000
    assert update.compact.trigger_input_tokens == 150_000
    assert update.reasoning.effort == "high"


def test_provider_form_accepts_short_protocol_names() -> None:
    update = ProviderForm(
        provider_id="gateway",
        protocol="openai",
        model="model-x",
    ).build_update()

    assert update.protocol is ProviderProtocol.OPENAI_RESPONSES
