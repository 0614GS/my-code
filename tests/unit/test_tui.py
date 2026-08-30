import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import StringIO
from time import monotonic

import pytest
from prompt_toolkit.data_structures import Size
from prompt_toolkit.formatted_text import fragment_list_to_text, to_formatted_text
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.layout import Window
from prompt_toolkit.layout.containers import VerticalAlign
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.color_depth import ColorDepth
from prompt_toolkit.utils import get_cwidth
from rich.console import Console, RenderableType
from rich.padding import Padding

from my_code.auth.credentials import CredentialSource
from my_code.chat.events import (
    CompactionCompleted,
    CompactionStarted,
    ContextUpdated,
    MaxStepsReached,
    ModelStepCompleted,
    ReasoningCompleted,
    ReasoningDelta,
    ReasoningStarted,
    TextCompleted,
    TextDelta,
    TextStarted,
    TodoListUpdated,
    ToolFinished,
    ToolStarted,
    TurnEvent,
    TurnSucceeded,
)
from my_code.chat.history import (
    HistoryContextGroup,
    HistoryContextItem,
    HistoryEntry,
    HistoryText,
    HistoryToolCall,
)
from my_code.chat.permissions import (
    PermissionModeSwitch,
    PermissionModeView,
    PermissionRequest,
)
from my_code.chat.status import ContextStatus, RuntimeStatus
from my_code.chat.views import CapabilitiesView, SessionView, SubagentTaskView
from my_code.config.providers import ProviderProtocol
from my_code.conversation.presentation import ToolResultPresentation
from my_code.features.todos.models import TodoItem
from my_code.model.capabilities import ModelDescriptor
from my_code.model.display import DisplayDensity
from my_code.model.primitives import ReasoningPresentation, TokenUsage
from my_code.permissions.models import PermissionConfirmation
from my_code.providers.manager import (
    ProviderProbeResult,
    ProviderUpdate,
    ProviderView,
)
from my_code.sessions.catalog import SessionSummary
from my_code.tools.presentation import ToolUsePresentation
from my_code.tui.activity import ToolActivityGroup
from my_code.tui.app import _STREAM_INVALIDATE_INTERVAL, MyCodeApp
from my_code.tui.commands import CommandConcurrency, SlashCommandRegistry
from my_code.tui.dimensions import SURFACE_VERTICAL_PADDING
from my_code.tui.presentation import format_context_usage, render_context_status
from my_code.tui.provider_screen import ProviderForm
from my_code.tui.terminal import NativeCursorVt100Output, terminal_color_depth
from my_code.tui.theme import TerminalPalette, TuiTheme
from my_code.tui.widgets import assistant_message, system_message, user_message


class FakeRuntime:
    def __init__(
        self,
        *,
        history: tuple[HistoryEntry, ...] = (),
        credential_source: CredentialSource = CredentialSource.STORED,
    ) -> None:
        self.prompts: list[str] = []
        self.history = history
        self.permission_handler = None
        self.submitted = asyncio.Event()
        self.provider_updates: list[ProviderUpdate] = []
        self.provider_selections: list[str] = []
        self.removed_credentials: list[str] = []
        self.has_stored_key = True
        self.credential_source = credential_source
        self.agents: tuple[SubagentTaskView, ...] = ()
        self.permission_mode = "default"
        self.full_access_confirmed = False
        self.sessions: tuple[SessionSummary, ...] = ()
        self.resumed_session_id: str | None = None
        self.view_density = DisplayDensity.CONCISE
        self.view_updates: list[DisplayDensity] = []

    async def initialize(self) -> SessionView:
        return SessionView(self.status(), self.history)

    def current_session_view(self) -> SessionView:
        return SessionView(self.status(), self.history)

    def view_mode(self) -> DisplayDensity:
        return self.view_density

    def set_view_mode(self, density: DisplayDensity) -> None:
        self.view_updates.append(density)
        self.view_density = density

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

    async def stream_compaction(self) -> AsyncIterator[TurnEvent]:
        yield CompactionStarted("manual")
        status = replace(self.context_status(), compact_count=1)
        yield CompactionCompleted("manual", TokenUsage(4, 2), status)

    def status(self) -> RuntimeStatus:
        return RuntimeStatus(
            session_id="session-id",
            cwd="/workspace",
            provider_id="anthropic",
            base_url=None,
            model="test-model",
            permission_mode=self.permission_mode,
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

    async def list_sessions(self) -> tuple[SessionSummary, ...]:
        return self.sessions

    async def resume_session(self, session_id: str) -> SessionView:
        self.resumed_session_id = session_id
        return self.current_session_view()

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

    async def configure_provider(
        self, update: ProviderUpdate, probe_result=None
    ) -> RuntimeStatus:
        del probe_result
        self.provider_updates.append(update)
        return self.status()

    async def select_provider(self, provider_id: str) -> RuntimeStatus:
        self.provider_selections.append(provider_id)
        return self.status()

    async def probe_provider(self, request) -> ProviderProbeResult:
        return ProviderProbeResult(
            True,
            (ModelDescriptor("model-x", "model-x"),),
            "2026-08-28T00:00:00+00:00",
            provider_id=request.provider_id,
            protocol=request.protocol,
            base_url=request.base_url,
        )

    async def remove_provider_credential(self, provider_id: str) -> RuntimeStatus:
        self.removed_credentials.append(provider_id)
        self.has_stored_key = False
        if self.credential_source is CredentialSource.STORED:
            self.credential_source = CredentialSource.NONE
        return self.status()

    async def suggest_paths(self, query: str):
        del query
        return ()

    def current_permission_mode(self) -> PermissionModeView:
        names = {
            "default": "Ask for me",
            "acceptEdits": "Approve edits",
            "bypassPermissions": "Full access",
        }
        return PermissionModeView(
            self.permission_mode,
            names[self.permission_mode],
            True,
            self.permission_mode == "bypassPermissions",
            False,
            self.permission_mode == "bypassPermissions"
            and not self.full_access_confirmed,
        )

    def permission_modes(self) -> tuple[PermissionModeView, ...]:
        names = {
            "default": "Ask for me",
            "acceptEdits": "Approve edits",
            "bypassPermissions": "Full access",
        }
        return tuple(
            PermissionModeView(
                value,
                name,
                value == self.permission_mode,
                value == "bypassPermissions",
                False,
                value == "bypassPermissions" and not self.full_access_confirmed,
            )
            for value, name in names.items()
        )

    def select_permission_mode(self, value: str) -> PermissionModeSwitch:
        mode = next(item for item in self.permission_modes() if item.value == value)
        needs_confirmation = mode.requires_confirmation
        changed = value != self.permission_mode and not needs_confirmation
        if changed:
            self.permission_mode = value
        return PermissionModeSwitch(mode, changed, needs_confirmation)

    def cycle_permission_mode(self) -> PermissionModeSwitch:
        order = ("default", "acceptEdits", "bypassPermissions")
        target = order[(order.index(self.permission_mode) + 1) % len(order)]
        needs_confirmation = (
            target == "bypassPermissions" and not self.full_access_confirmed
        )
        if not needs_confirmation:
            self.permission_mode = target
        view = PermissionModeView(
            target,
            {
                "default": "Ask for me",
                "acceptEdits": "Approve edits",
                "bypassPermissions": "Full access",
            }[target],
            not needs_confirmation,
            target == "bypassPermissions",
            False,
            needs_confirmation,
        )
        return PermissionModeSwitch(view, not needs_confirmation, needs_confirmation)

    def confirm_full_access(self, allow: bool) -> PermissionModeView:
        if allow:
            self.full_access_confirmed = True
            self.permission_mode = "bypassPermissions"
        elif self.permission_mode == "bypassPermissions":
            self.permission_mode = "default"
        return self.current_permission_mode()


class RecordingOutput(DummyOutput):
    def __init__(self) -> None:
        self.erase_down_calls = 0

    def erase_down(self) -> None:
        self.erase_down_calls += 1


class RecordingMyCodeApp(MyCodeApp):
    def __init__(self, runtime: FakeRuntime) -> None:
        super().__init__(
            runtime,  # type: ignore[arg-type]
            output=DummyOutput(),
            console=Console(file=StringIO(), force_terminal=False),
        )
        self.write_snapshots: list[tuple[str, tuple[str, ...], RenderableType]] = []

    async def _write(self, renderable: RenderableType, *, clear: bool = False) -> None:
        del clear
        self.write_snapshots.append(
            (self._stream_text, tuple(self._reasoning_parts), renderable)
        )


class InvalidateRecordingApp(RecordingMyCodeApp):
    """Records _invalidate calls instead of forwarding to prompt_toolkit."""

    def __init__(self, runtime: FakeRuntime) -> None:
        super().__init__(runtime)
        self.invalidate_calls = 0

    def _invalidate(self) -> None:
        self.invalidate_calls += 1


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
        pipe.send_text("first\nsecond\r")
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
async def test_multiline_composer_aligns_continuations_after_prompt() -> None:
    with create_pipe_input() as pipe:
        app = MyCodeApp(
            FakeRuntime(),  # type: ignore[arg-type]
            input=pipe,
            output=DummyOutput(),
            console=Console(file=StringIO(), force_terminal=False),
        )
        running = asyncio.create_task(app.run_async())
        await asyncio.sleep(0.05)
        pipe.send_text("first\nsecond")
        for _ in range(20):
            if app.buffer.text == "first\nsecond":
                break
            await asyncio.sleep(0.01)

        content = app.input_control.create_content(width=80, height=8)
        lines = [
            fragment_list_to_text(content.get_line(index)).rstrip()
            for index in range(content.line_count)
        ]
        assert lines == ["› first", "  second"]

        pipe.send_bytes(b"\x03\x04")
        await running


@pytest.mark.asyncio
async def test_exit_erases_dynamic_composer_region() -> None:
    runtime = FakeRuntime()
    output = RecordingOutput()
    with create_pipe_input() as pipe:
        app = MyCodeApp(
            runtime,  # type: ignore[arg-type]
            input=pipe,
            output=output,
            console=Console(file=StringIO(), force_terminal=False),
        )
        running = asyncio.create_task(app.run_async())
        await asyncio.sleep(0.05)
        erase_calls_before_exit = output.erase_down_calls

        pipe.send_bytes(b"\x04")
        await running

    assert output.erase_down_calls == erase_calls_before_exit + 1


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


@pytest.mark.asyncio
async def test_startup_keeps_composer_visible_and_queues_one_submission() -> None:
    class BlockingRuntime(FakeRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.initializing = asyncio.Event()
            self.release = asyncio.Event()

        async def initialize(self) -> SessionView:
            self.initializing.set()
            await self.release.wait()
            return SessionView(self.status(), self.history)

    runtime = BlockingRuntime()
    stream = StringIO()
    with create_pipe_input() as pipe:
        app = MyCodeApp(
            runtime,  # type: ignore[arg-type]
            input=pipe,
            output=DummyOutput(),
            console=Console(file=stream, width=100, force_terminal=False),
        )
        running = asyncio.create_task(app.run_async())
        await asyncio.wait_for(runtime.initializing.wait(), 1)

        assert "my-code" in stream.getvalue()
        assert "Capabilities are initializing" not in stream.getvalue()
        assert "Initializing capabilities" in fragment_list_to_text(
            to_formatted_text(app._activity_text())
        )
        assert app._composer_read_only() is False

        pipe.send_text("queued prompt\r\r")
        await asyncio.sleep(0.05)
        assert runtime.prompts == []
        assert app.buffer.text == "queued prompt"

        runtime.release.set()
        await asyncio.wait_for(runtime.submitted.wait(), 1)
        assert runtime.prompts == ["queued prompt"]
        for _ in range(20):
            if not app._busy and app._foreground_task is None:
                break
            await asyncio.sleep(0.01)
        pipe.send_bytes(b"\x04")
        await running

    assert "Ready · 5 tools · 1 skills" in stream.getvalue()


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
    rendered = stream.getvalue()
    assert "/status" in rendered
    assert "╭" in rendered and "╰" in rendered
    assert ">_ my-code v" in rendered
    assert "Session:" in rendered


@pytest.mark.asyncio
async def test_status_command_uses_cached_context_during_an_open_tool_pair() -> None:
    class InFlightRuntime(FakeRuntime):
        def context_status(self) -> ContextStatus:
            raise ValueError("Unresolved tool use in model input: call_01")

    runtime = InFlightRuntime()
    stream = StringIO()
    app = MyCodeApp(
        runtime,  # type: ignore[arg-type]
        output=DummyOutput(),
        console=Console(file=stream, width=80, force_terminal=False),
    )
    app._context_status = FakeRuntime().context_status()
    outcome = app.commands.dispatch("/status", status=runtime.status())
    assert outcome is not None

    await app._handle_command(outcome, command_line="/status")

    rendered = stream.getvalue()
    assert rendered.count("/status") == 1
    assert "Context window:" in rendered


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


def test_footer_hides_context_until_a_user_action_measures_it() -> None:
    runtime = FakeRuntime()
    app = MyCodeApp(
        runtime,  # type: ignore[arg-type]
        output=DummyOutput(),
        console=Console(file=StringIO(), force_terminal=False),
    )
    app._status = runtime.status()

    before = fragment_list_to_text(to_formatted_text(app._status_display()))
    app._apply_context_update(runtime.context_status())
    after = fragment_list_to_text(to_formatted_text(app._status_display()))

    assert "/ 200k" not in before
    assert "0.1k / 200k" in after


def test_footer_omits_permission_mode_and_shift_tab_hint() -> None:
    runtime = FakeRuntime()
    app = MyCodeApp(
        runtime,  # type: ignore[arg-type]
        output=DummyOutput(),
        console=Console(file=StringIO(), width=100, force_terminal=False),
    )
    runtime.permission_mode = "bypassPermissions"
    runtime.full_access_confirmed = True
    app._status = runtime.status()
    app._context_status = runtime.context_status()

    footer = fragment_list_to_text(to_formatted_text(app._status_display()))

    assert "Full access" not in footer
    assert "bypassPermissions" not in footer
    assert "Shift+Tab" not in footer


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


def test_command_picker_uses_the_same_host_below_composer() -> None:
    app = MyCodeApp(
        FakeRuntime(),  # type: ignore[arg-type]
        output=DummyOutput(),
        console=Console(file=StringIO(), force_terminal=False),
    )
    app._sessions = (SessionSummary("session-1", "Visible session", datetime.now(UTC)),)
    app._open_panel("resume")

    composer_index = next(
        index
        for index, child in enumerate(app.body.children)
        if getattr(child, "content", None) is app.input_control
    )
    interaction_index = app.body.children.index(app.interaction_menu)

    assert interaction_index > composer_index
    assert "Resume a conversation" in fragment_list_to_text(
        to_formatted_text(app._interaction_text())
    )
    assert "Resume a conversation" not in fragment_list_to_text(
        to_formatted_text(app._dynamic_text())
    )


def test_dynamic_layout_naturally_follows_terminal_scrollback() -> None:
    app = MyCodeApp(
        FakeRuntime(),  # type: ignore[arg-type]
        output=DummyOutput(),
        console=Console(file=StringIO(), force_terminal=False),
    )

    assert app.body.align is VerticalAlign.JUSTIFY


@pytest.mark.asyncio
async def test_completed_markdown_retires_live_projection_before_scrollback() -> None:
    app = RecordingMyCodeApp(FakeRuntime())
    markdown = "# Result\n\n" + "\n".join(f"- item {index}" for index in range(20))

    async def events() -> AsyncIterator[TurnEvent]:
        yield TextStarted()
        yield TextDelta(markdown)
        yield TextCompleted(markdown)
        yield ReasoningStarted("summary")
        yield ReasoningDelta("summary", 0, "safe reasoning")
        yield ReasoningCompleted(ReasoningPresentation("summary", ("safe reasoning",)))
        yield TurnSucceeded(markdown, 1, 10, 2)

    await app._run_turn("", events(), user=False)

    assert len(app.write_snapshots) == 3
    assert all(not stream_text for stream_text, _, _ in app.write_snapshots)
    assert all(not reasoning for _, reasoning, _ in app.write_snapshots)


@pytest.mark.asyncio
async def test_context_status_updates_between_model_steps() -> None:
    runtime = FakeRuntime()
    app = RecordingMyCodeApp(runtime)
    app._status = runtime.status()
    step_status = replace(
        runtime.context_status(),
        input_tokens=4_400,
        context_entry_count=17,
        conversation_entry_count=19,
    )

    async def events() -> AsyncIterator[TurnEvent]:
        yield ContextUpdated(step_status)
        assert app._context_status is step_status
        assert app._status is not None
        assert app._status.context_entry_count == 17
        assert "4.4k / 200k" in app._status_text()
        yield TurnSucceeded("done", 2, 10, 2)

    await app._run_turn("", events(), user=False)


@pytest.mark.asyncio
async def test_success_fallback_retires_partial_markdown_before_scrollback() -> None:
    app = RecordingMyCodeApp(FakeRuntime())

    async def events() -> AsyncIterator[TurnEvent]:
        yield TextDelta("partial **answer**")
        yield TurnSucceeded("partial **answer**", 1, 10, 2)

    await app._run_turn("", events(), user=False)

    assert len(app.write_snapshots) == 2
    assert all(not stream_text for stream_text, _, _ in app.write_snapshots)


@pytest.mark.asyncio
async def test_max_steps_retires_partial_projection_before_scrollback() -> None:
    app = RecordingMyCodeApp(FakeRuntime())

    async def events() -> AsyncIterator[TurnEvent]:
        yield TextDelta("partial answer")
        yield ReasoningDelta("summary", 0, "partial reasoning")
        yield MaxStepsReached(10, 10, 100, 20)

    await app._run_turn("", events(), user=False)

    assert len(app.write_snapshots) == 2
    assert all(not stream_text for stream_text, _, _ in app.write_snapshots)
    assert all(not reasoning for _, reasoning, _ in app.write_snapshots)
    assert app._activity_indicator.active is False


@pytest.mark.asyncio
async def test_failed_turn_retires_partial_projection_before_writes() -> None:
    app = RecordingMyCodeApp(FakeRuntime())

    async def events() -> AsyncIterator[TurnEvent]:
        yield TextDelta("partial answer")
        yield ReasoningDelta("summary", 0, "partial reasoning")
        raise RuntimeError("stream failed")

    await app._run_turn("", events(), user=False)

    assert len(app.write_snapshots) == 2
    assert all(not stream_text for stream_text, _, _ in app.write_snapshots)
    assert all(not reasoning for _, reasoning, _ in app.write_snapshots)
    assert app._activity_indicator.active is False


@pytest.mark.asyncio
async def test_activity_ticker_only_invalidates_while_visible_and_stops() -> None:
    app = InvalidateRecordingApp(FakeRuntime())
    app._running = True
    owner = app._begin_activity("Working")
    baseline = app.invalidate_calls
    ticker = asyncio.create_task(app._tick_activity())

    await asyncio.sleep(0.22)
    assert app.invalidate_calls > baseline

    app._end_activity(owner)
    stopped_at = app.invalidate_calls
    await asyncio.sleep(0.15)
    assert app.invalidate_calls == stopped_at

    app._running = False
    app._activity_changed.set()
    await asyncio.wait_for(ticker, 1)


@pytest.mark.asyncio
async def test_cancelled_turn_retires_partial_projection_before_writes() -> None:
    app = RecordingMyCodeApp(FakeRuntime())

    async def events() -> AsyncIterator[TurnEvent]:
        yield TextDelta("partial answer")
        yield ReasoningDelta("summary", 0, "partial reasoning")
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await app._run_turn("", events(), user=False)

    assert len(app.write_snapshots) == 2
    assert all(not stream_text for stream_text, _, _ in app.write_snapshots)
    assert all(not reasoning for _, reasoning, _ in app.write_snapshots)
    assert app._activity_indicator.active is False


@pytest.mark.asyncio
async def test_streaming_invalidate_coalesces_rapid_deltas() -> None:
    """Multiple deltas within one frame window trigger at most two redraws."""
    app = InvalidateRecordingApp(FakeRuntime())
    app._running = True  # flush guard only fires while the host is running
    # The first delta after construction redraws immediately: the elapsed
    # window starts exhausted because _last_stream_invalidate is 0.0.
    app._invalidate_for_event(TextDelta("a"))
    assert app.invalidate_calls == 1
    assert app._stream_invalidate_pending is False

    # Deltas arriving within the frame window merge into a single deferred
    # flush rather than each triggering their own redraw.
    for char in "bcdefghij":
        app._invalidate_for_event(TextDelta(char))
    assert app.invalidate_calls == 1
    assert app._stream_invalidate_pending is True

    # The deferred flush fires once the window elapses.
    await asyncio.sleep(_STREAM_INVALIDATE_INTERVAL + 0.02)
    assert app.invalidate_calls == 2
    assert app._stream_invalidate_pending is False


@pytest.mark.asyncio
async def test_structural_event_bypasses_streaming_throttle() -> None:
    """TurnSucceeded (and other structural events) redraw immediately."""
    app = InvalidateRecordingApp(FakeRuntime())
    app._running = True
    # Prime the throttle so a follow-up delta would be deferred.
    app._invalidate_for_event(TextDelta("a"))
    assert app.invalidate_calls == 1

    app._invalidate_for_event(TextDelta("b"))
    assert app._stream_invalidate_pending is True
    assert app.invalidate_calls == 1

    # A structural event must not wait for the pending streaming flush.
    app._invalidate_for_event(TurnSucceeded("done", 1, 10, 2))
    assert app.invalidate_calls == 2
    assert app._stream_invalidate_pending is True

    # Drain the scheduled flush so it does not leak across tests.
    await asyncio.sleep(_STREAM_INVALIDATE_INTERVAL + 0.02)
    assert app.invalidate_calls == 3
    assert app._stream_invalidate_pending is False


@pytest.mark.asyncio
async def test_deferred_stream_flush_is_skipped_after_shutdown() -> None:
    """A pending flush scheduled before shutdown must not redraw afterwards."""
    app = InvalidateRecordingApp(FakeRuntime())
    app._running = True
    app._invalidate_for_event(TextDelta("a"))  # immediate redraw
    app._invalidate_for_event(TextDelta("b"))  # schedules a deferred flush
    assert app._stream_invalidate_pending is True
    assert app.invalidate_calls == 1

    app._running = False  # shutdown before the flush can fire
    await asyncio.sleep(_STREAM_INVALIDATE_INTERVAL + 0.02)
    assert app.invalidate_calls == 1  # flush skipped by the _running guard
    assert app._stream_invalidate_pending is False


@pytest.mark.asyncio
async def test_background_completion_retires_live_projection_before_scrollback() -> (
    None
):
    app = RecordingMyCodeApp(FakeRuntime())
    app._stream_text = "background answer"
    app._reasoning_parts = ["background reasoning"]

    await app._consume_background_event(TextCompleted("background answer"))
    await app._consume_background_event(ModelStepCompleted(1, False))

    assert len(app.write_snapshots) == 1
    assert app.write_snapshots[0][:2] == ("", ("background reasoning",))


@pytest.mark.asyncio
async def test_turn_groups_tools_in_start_order_even_when_completion_is_reversed() -> (
    None
):
    app = RecordingMyCodeApp(FakeRuntime())

    async def events() -> AsyncIterator[TurnEvent]:
        yield ToolStarted(
            "read", ToolUsePresentation("Read", "a.py", "Reading", "explore")
        )
        yield ToolStarted(
            "bash", ToolUsePresentation("Bash", "pytest", "Running", "command")
        )
        yield ToolFinished("bash", False, ToolResultPresentation("passed"))
        yield ToolFinished("read", False, ToolResultPresentation("10 lines"))
        yield TextStarted()
        yield TextCompleted("done")
        yield TurnSucceeded("done", 1, 10, 2)

    await app._run_turn("", events(), user=False)

    stream = StringIO()
    console = Console(file=stream, width=80, force_terminal=False)
    console.print(app.write_snapshots[0][2])
    rendered = stream.getvalue()
    assert rendered.index("Read") < rendered.index("Bash")
    assert len(app.write_snapshots) == 3


@pytest.mark.asyncio
async def test_successful_todo_write_only_emits_changed_plan_snapshot() -> None:
    app = RecordingMyCodeApp(FakeRuntime())
    todos = (TodoItem("Run tests", "in_progress", "Running tests"),)

    async def events() -> AsyncIterator[TurnEvent]:
        yield ToolStarted(
            "todo",
            ToolUsePresentation("Update Todos", "1 item", "Updating todos"),
        )
        yield ToolFinished("todo", False, ToolResultPresentation("updated"))
        yield TodoListUpdated(todos)
        yield TurnSucceeded("", 1, 10, 2)

    await app._run_turn("", events(), user=False)

    stream = StringIO()
    console = Console(file=stream, width=80, force_terminal=False)
    for _, _, renderable in app.write_snapshots:
        console.print(renderable)
    rendered = stream.getvalue()
    assert rendered.count("Updated Plan") == 1
    assert "Update Todos" not in rendered
    assert "Run tests" in rendered


@pytest.mark.asyncio
async def test_resume_history_rebuilds_todo_snapshot_without_tool_row() -> None:
    app = RecordingMyCodeApp(FakeRuntime())
    todos = (TodoItem("Run tests", "in_progress", "Running tests"),)
    history = (
        HistoryToolCall(
            "todo",
            ToolUsePresentation("Update Todos", "1 item", "Updating todos"),
            ToolResultPresentation("updated"),
            False,
            todos=todos,
            ends_tool_batch=True,
        ),
    )

    await app._render_history(history)

    stream = StringIO()
    Console(file=stream, force_terminal=False).print(app.write_snapshots[0][2])
    rendered = stream.getvalue()
    assert "Updated Plan" in rendered
    assert "Update Todos" not in rendered


def test_user_message_matches_composer_vertical_padding() -> None:
    theme = TuiTheme(TerminalPalette((48, 10, 36)))
    message = user_message("hello", theme)

    assert isinstance(message, Padding)
    assert message.expand is True
    assert message.style == "on #49273e"
    assert message.top == SURFACE_VERTICAL_PADDING
    assert message.bottom == SURFACE_VERTICAL_PADDING


@pytest.mark.asyncio
async def test_scrollback_blocks_have_exactly_one_blank_line_between_them() -> None:
    stream = StringIO()
    app = MyCodeApp(
        FakeRuntime(),  # type: ignore[arg-type]
        output=DummyOutput(),
        console=Console(file=stream, width=40, force_terminal=False),
    )

    await app._write(user_message("hello", app.theme))
    await app._write(assistant_message("answer"))
    await app._write(system_message("done"))

    lines = [line.rstrip() for line in stream.getvalue().splitlines()]
    assert lines == ["", " › hello", "", "answer", "", "done"]


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


def test_work_indicator_is_below_tool_activity_in_its_own_row() -> None:
    app = MyCodeApp(
        FakeRuntime(),  # type: ignore[arg-type]
        output=DummyOutput(),
        console=Console(file=StringIO(), width=80, force_terminal=False),
    )
    app._begin_agent_activity("my-code is working…")
    app._tool_activity = ToolActivityGroup()
    app._tool_activity.start(
        "read-1",
        ToolUsePresentation(
            display_name="Read",
            activity="Reading file",
            summary="src/example.py",
        ),
    )

    dynamic = fragment_list_to_text(to_formatted_text(app._dynamic_text()))
    activity = fragment_list_to_text(to_formatted_text(app._activity_text()))

    assert "Read" in dynamic
    assert "my-code is working…" not in dynamic
    assert "my-code is working…" in activity
    app._end_agent_activity()


@pytest.mark.asyncio
async def test_manual_compaction_shows_progress_then_commits_status() -> None:
    class BlockingCompactRuntime(FakeRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.compact_count = 0

        def context_status(self) -> ContextStatus:
            return replace(super().context_status(), compact_count=self.compact_count)

        async def stream_compaction(self) -> AsyncIterator[TurnEvent]:
            yield CompactionStarted("manual")
            self.started.set()
            await self.release.wait()
            self.compact_count = 1
            status = self.context_status()
            yield CompactionCompleted("manual", TokenUsage(4, 2), status)

    runtime = BlockingCompactRuntime()
    stream = StringIO()
    app = MyCodeApp(
        runtime,  # type: ignore[arg-type]
        output=DummyOutput(),
        console=Console(file=stream, width=80, force_terminal=False),
    )
    task = asyncio.create_task(app._run_compaction())
    await runtime.started.wait()

    progress = fragment_list_to_text(to_formatted_text(app._activity_text()))
    assert "Compacting context…" in progress
    assert "0s" in progress

    runtime.release.set()
    await task

    assert app._activity_indicator.active is False
    assert app._context_status is not None
    assert app._context_status.compact_count == 1
    assert "Context compacted · manual" in stream.getvalue()


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
async def test_resume_picker_keeps_every_selected_session_visible_and_resumable() -> (
    None
):
    runtime = FakeRuntime()
    now = datetime.now(UTC)
    runtime.sessions = tuple(
        SessionSummary(
            f"session-{index}",
            f"Conversation {index}",
            now - timedelta(minutes=index),
        )
        for index in range(10)
    )
    app = MyCodeApp(
        runtime,  # type: ignore[arg-type]
        output=DummyOutput(),
        console=Console(file=StringIO(), force_terminal=False),
    )
    await app._open_resume()

    app._move_panel(-1)
    assert app._panel_index == 0
    for _ in range(20):
        app._move_panel(1)

    assert app._panel_index == 9
    rendered = fragment_list_to_text(to_formatted_text(app._panel_text()))
    assert "Conversation 9" in rendered
    assert "Conversation 0" not in rendered
    assert "10/10" in rendered

    await app._panel_enter()

    assert runtime.resumed_session_id == "session-9"
    assert app._panel is None


def test_resume_picker_right_aligns_relative_times() -> None:
    now = datetime.now(UTC)
    app = MyCodeApp(
        FakeRuntime(),  # type: ignore[arg-type]
        output=DummyOutput(),
        console=Console(file=StringIO(), force_terminal=False, width=50),
    )
    app._sessions = (
        SessionSummary("short", "短对话", now),
        SessionSummary("long", "A considerably longer conversation", now),
    )
    app._panel = "resume"

    lines = fragment_list_to_text(to_formatted_text(app._panel_text())).splitlines()
    session_lines = [line for line in lines if line.startswith("  ")]

    assert len(session_lines) == 2
    assert all(get_cwidth(line) == 50 for line in session_lines)
    assert all(line.endswith("just now") for line in session_lines)


@pytest.mark.asyncio
async def test_provider_and_model_pickers_do_not_truncate_long_lists() -> None:
    app = MyCodeApp(
        FakeRuntime(),  # type: ignore[arg-type]
        output=DummyOutput(),
        console=Console(file=StringIO(), force_terminal=False),
    )
    app._providers = tuple(
        ProviderView(
            f"provider-{index}",
            ProviderProtocol.ANTHROPIC_MESSAGES,
            f"model-{index}",
            None,
            index == 0,
            False,
            CredentialSource.NONE,
        )
        for index in range(10)
    )
    app._panel = "provider_select"
    app._panel_index = 0
    for _ in range(9):
        app._move_panel(1)

    providers = fragment_list_to_text(to_formatted_text(app._panel_text()))
    assert "provider-9" in providers
    assert "provider-0" not in providers
    await app._panel_enter()
    assert app._provider_selected_index == 9

    app._provider_form = ProviderForm(model="model-0")
    app._provider_models = tuple(f"model-{index}" for index in range(10))
    app._panel = "provider_models"
    app._panel_index = 0
    for _ in range(9):
        app._move_panel(1)

    models = fragment_list_to_text(to_formatted_text(app._panel_text()))
    assert "model-9" in models
    assert "model-0" not in models
    await app._panel_enter()
    assert app._provider_form.model == "model-9"


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
async def test_real_arrow_sequences_keep_long_resume_selection_visible() -> None:
    runtime = FakeRuntime()
    now = datetime.now(UTC)
    runtime.sessions = tuple(
        SessionSummary(f"session-{index}", f"Session {index}", now)
        for index in range(10)
    )
    with create_pipe_input() as pipe:
        app = MyCodeApp(
            runtime,  # type: ignore[arg-type]
            input=pipe,
            output=DummyOutput(),
            console=Console(file=StringIO(), force_terminal=False),
        )
        running = asyncio.create_task(app.run_async())
        await asyncio.sleep(0.05)
        pipe.send_text("/resume\r")
        for _ in range(40):
            if app._panel == "resume":
                break
            await asyncio.sleep(0.01)

        assert app._panel == "resume"
        pipe.send_bytes(b"\x1b[B" * 8)
        await asyncio.sleep(0.05)

        assert app._panel_index == 8
        rendered = fragment_list_to_text(to_formatted_text(app._interaction_text()))
        assert "Session 8" in rendered
        assert "Session 0" not in rendered

        pipe.send_bytes(b"\x1b\x03\x04")
        await running


@pytest.mark.asyncio
async def test_escape_is_responsive_and_ctrl_j_inserts_newline() -> None:
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

        started = monotonic()
        pipe.send_bytes(b"\x1b")
        for _ in range(30):
            if not app._slash_active():
                break
            await asyncio.sleep(0.01)

        assert not app._slash_active()
        assert monotonic() - started < 0.2

        app.buffer.reset()
        pipe.send_text("line")
        pipe.send_bytes(b"\n")
        for _ in range(20):
            if app.buffer.text == "line\n":
                break
            await asyncio.sleep(0.01)
        assert app.buffer.text == "line\n"

        pipe.send_bytes(b"\x03\x04")
        await running


@pytest.mark.asyncio
@pytest.mark.parametrize("sequence", (b"\x1b[13;2u", b"\x1b[27;2;13~"))
async def test_enhanced_terminal_shift_enter_inserts_newline(sequence: bytes) -> None:
    with create_pipe_input() as pipe:
        app = MyCodeApp(
            FakeRuntime(),  # type: ignore[arg-type]
            input=pipe,
            output=DummyOutput(),
            console=Console(file=StringIO(), force_terminal=False),
        )
        running = asyncio.create_task(app.run_async())
        await asyncio.sleep(0.05)
        pipe.send_text("line")
        pipe.send_bytes(sequence)
        for _ in range(20):
            if app.buffer.text == "line\n":
                break
            await asyncio.sleep(0.01)

        assert app.buffer.text == "line\n"

        pipe.send_bytes(b"\x03\x04")
        await running


@pytest.mark.asyncio
async def test_escape_then_enter_no_longer_inserts_newline() -> None:
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
        pipe.send_text("submit me")
        pipe.send_bytes(b"\x1b\r")
        await asyncio.wait_for(runtime.submitted.wait(), timeout=0.5)

        assert runtime.prompts == ["submit me"]

        for _ in range(50):
            if not app._busy:
                break
            await asyncio.sleep(0.01)

        pipe.send_bytes(b"\x04")
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
    assert runtime.provider_selections == ["anthropic"]
    assert runtime.provider_updates == []
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
async def test_provider_credential_removal_reports_unconfigured_provider() -> None:
    runtime = FakeRuntime(credential_source=CredentialSource.STORED)
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
    assert "stored" in actions
    assert "Remove saved API key" in actions
    app._panel_index = 2
    await app._panel_enter()
    app._panel_index = 0
    await app._panel_enter()

    assert runtime.removed_credentials == ["anthropic"]
    assert app._panel is None
    assert app.buffer.text == "draft"
    assert "now not configured" in output.getvalue()


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
    runtime = FakeRuntime()
    app = MyCodeApp(
        runtime,  # type: ignore[arg-type]
        output=DummyOutput(),
        console=Console(file=StringIO(), force_terminal=False),
    )
    app._open_provider()
    app._panel_index = 1
    await app._panel_enter()
    assert app._panel == "provider_protocol"
    app._panel_index = 1
    await app._panel_enter()
    core_values = (
        "gateway",
        "https://example.test/v1",
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
    assert registry.dispatch("/model", status=status).open_model_picker  # type: ignore[union-attr]
    assert registry.dispatch("/model anything", status=status).message == (  # type: ignore[union-attr]
        "/model does not accept arguments."
    )
    skills = registry.dispatch("/skills reload", status=status)
    mcp = registry.dispatch("/mcp refresh local", status=status)
    assert skills is not None and skills.skill_operation == "reload"
    assert mcp is not None and mcp.mcp_operation == ("refresh", "local")
    assert registry.dispatch("/tasks", status=status).show_tasks  # type: ignore[union-attr]
    invalid = registry.dispatch("/mcp refresh", status=status)
    assert invalid is not None and invalid.message.startswith("Usage:")
    assert registry.dispatch("/view", status=status).open_view_picker  # type: ignore[union-attr]
    assert registry.dispatch("/view detailed", status=status).view_operation == (  # type: ignore[union-attr]
        "detailed"
    )
    invalid_view = registry.dispatch("/view audit", status=status)
    assert invalid_view is not None and invalid_view.message == (
        "Usage: /view [concise|detailed]"
    )
    assert registry.concurrency("/view detailed") is CommandConcurrency.CONCURRENT_UI
    permissions = registry.dispatch("/permissions", status=status)
    assert permissions is not None and permissions.open_permission_picker
    assert registry.concurrency("/permissions") is CommandConcurrency.EXCLUSIVE


@pytest.mark.asyncio
async def test_permissions_command_uses_shared_picker_and_applies_selection() -> None:
    runtime = FakeRuntime()
    app = MyCodeApp(
        runtime,  # type: ignore[arg-type]
        output=DummyOutput(),
        console=Console(file=StringIO(), width=100, force_terminal=False),
    )
    app.buffer.text = "/permissions"

    outcome = app.commands.dispatch("/permissions", status=runtime.status())
    assert outcome is not None
    await app._handle_command(outcome, command_line="/permissions")

    assert app._panel == "permission_mode_select"
    rendered = fragment_list_to_text(to_formatted_text(app._panel_text()))
    assert "Choose permission mode" in rendered
    assert "● Ask for me" in rendered
    assert "Approve edits" in rendered
    assert "Full access" in rendered

    app._panel_index = 1
    await app._panel_enter()

    assert app._panel is None
    assert runtime.permission_mode == "acceptEdits"
    assert app.buffer.text == "/permissions"


@pytest.mark.asyncio
async def test_permissions_full_access_keeps_shared_confirmation_flow() -> None:
    runtime = FakeRuntime()
    app = MyCodeApp(runtime)  # type: ignore[arg-type]
    app.buffer.text = "draft"
    app._open_permission_picker()
    app._panel_index = 2

    await app._panel_enter()

    assert app._panel == "full_access"
    assert app._panel_index == 0
    assert runtime.permission_mode == "default"
    app._resolve_full_access(False)
    assert runtime.permission_mode == "default"
    assert app.buffer.text == "draft"


@pytest.mark.asyncio
async def test_permissions_picker_keeps_old_mode_open_when_selection_fails() -> None:
    class FailingPermissionRuntime(FakeRuntime):
        def select_permission_mode(self, value: str) -> PermissionModeSwitch:
            del value
            raise OSError("session is read-only")

    runtime = FailingPermissionRuntime()
    app = MyCodeApp(runtime)  # type: ignore[arg-type]
    app._open_permission_picker()
    app._panel_index = 1

    await app._panel_enter()

    assert app._panel == "permission_mode_select"
    assert runtime.permission_mode == "default"


@pytest.mark.asyncio
async def test_view_command_opens_shared_picker_and_applies_selection() -> None:
    runtime = FakeRuntime()
    app = MyCodeApp(
        runtime,  # type: ignore[arg-type]
        output=DummyOutput(),
        console=Console(file=StringIO(), width=100, force_terminal=False),
    )

    outcome = app.commands.dispatch("/view", status=runtime.status())
    assert outcome is not None
    await app._handle_command(outcome, command_line="/view")

    assert app._panel == "view_select"
    rendered = fragment_list_to_text(to_formatted_text(app._panel_text()))
    assert "Choose output detail" in rendered
    assert "● Concise" in rendered
    assert "Detailed" in rendered

    app._panel_index = 1
    await app._panel_enter()

    assert app._panel is None
    assert runtime.view_updates == [DisplayDensity.DETAILED]
    assert app._display_density is DisplayDensity.DETAILED


@pytest.mark.asyncio
async def test_view_picker_keeps_old_mode_open_when_setting_write_fails() -> None:
    class FailingViewRuntime(FakeRuntime):
        def set_view_mode(self, density: DisplayDensity) -> None:
            del density
            raise OSError("settings are read-only")

    runtime = FailingViewRuntime()
    app = MyCodeApp(
        runtime,  # type: ignore[arg-type]
        output=DummyOutput(),
        console=Console(file=StringIO(), force_terminal=False),
    )
    app._open_view_picker()
    app._panel_index = 1

    await app._panel_enter()

    assert app._panel == "view_select"
    assert app._display_density is DisplayDensity.CONCISE


@pytest.mark.asyncio
async def test_view_switch_clears_and_rerenders_detailed_session_history() -> None:
    history: tuple[HistoryEntry, ...] = (
        HistoryText("user", "question"),
        HistoryContextGroup(
            1,
            (HistoryContextItem("AGENTS context", "agents", "injected rules"),),
        ),
        HistoryText("assistant", "answer", is_final_answer=True),
    )
    runtime = FakeRuntime(history=history)

    class RerenderRecordingApp(RecordingMyCodeApp):
        def __init__(self, runtime: FakeRuntime) -> None:
            super().__init__(runtime)
            self.clear_flags: list[bool] = []

        async def _write(
            self, renderable: RenderableType, *, clear: bool = False
        ) -> None:
            self.clear_flags.append(clear)
            await super()._write(renderable, clear=clear)

    app = RerenderRecordingApp(runtime)

    message = await app._change_view_mode("detailed")

    stream = StringIO()
    console = Console(file=stream, width=80, force_terminal=False)
    for _, _, renderable in app.write_snapshots:
        console.print(renderable)
    rendered = stream.getvalue()
    assert app.clear_flags[0]
    assert "Injected context · request #1" in rendered
    assert "injected rules" in rendered
    assert "User input" in rendered
    assert "Assistant response" in rendered
    assert message.endswith("session re-rendered")


@pytest.mark.asyncio
async def test_concise_history_hides_detailed_context_groups() -> None:
    app = RecordingMyCodeApp(FakeRuntime())
    history: tuple[HistoryEntry, ...] = (
        HistoryText("user", "question"),
        HistoryContextGroup(
            1,
            (HistoryContextItem("AGENTS context", None, "injected rules"),),
        ),
        HistoryText("assistant", "answer", is_final_answer=True),
    )

    await app._render_history(history)

    stream = StringIO()
    console = Console(file=stream, force_terminal=False)
    for _, _, renderable in app.write_snapshots:
        console.print(renderable)
    assert "injected rules" not in stream.getvalue()


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


@pytest.mark.asyncio
async def test_permission_modal_restores_picker_cursor_and_viewport() -> None:
    runtime = FakeRuntime()
    runtime.agents = tuple(
        subagent_view(f"agent-{index}", f"Agent {index}") for index in range(10)
    )
    app = MyCodeApp(
        runtime,  # type: ignore[arg-type]
        output=DummyOutput(),
        console=Console(file=StringIO(), force_terminal=False),
    )
    app._open_agents()
    for _ in range(8):
        app._move_panel(1)
    app._panel_text()
    selected_key = app._panel_picker.selected_key
    offset = app._panel_picker.offset

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
    app._choose_permission("deny")
    assert await pending == PermissionConfirmation(False)

    assert app._panel == "agents"
    assert app._panel_picker.selected_key == selected_key
    assert app._panel_picker.offset == offset


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
