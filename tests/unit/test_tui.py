import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from rich.console import Console
from textual.widgets import Input, OptionList

from nano_code.permissions import PermissionConfirmation
from nano_code.presentation import ToolResultPresentation, ToolUsePresentation
from nano_code.providers.manager import ProviderUpdate, ProviderView
from nano_code.providers.profiles import ProviderProtocol
from nano_code.sessions import SessionSummary
from nano_code.todos import TodoItem
from nano_code.tui import (
    ContextStatus,
    HistoryAssistantMessage,
    HistoryUserMessage,
    NanoCodeApp,
    PermissionHandler,
    ProviderScreen,
    ResumedSession,
    ResumeScreen,
    RuntimeStatus,
    TodoListUpdated,
)
from nano_code.tui.commands import SlashCommandRegistry
from nano_code.tui.contracts import (
    MaxTurnsReached,
    PermissionRequest,
    TextDelta,
    ToolFinished,
    ToolStarted,
    TurnCompleted,
    TurnEvent,
    TurnLimitReached,
    TurnOutcome,
    TurnSucceeded,
)
from nano_code.tui.widgets import (
    ActivityBar,
    AssistantMessage,
    PermissionPanel,
    SystemMessage,
    TodoPanel,
    ToolCallMessage,
    UserMessage,
)


class FakeRuntime:
    def __init__(self, *, request_permission: bool = False) -> None:
        self.prompts: list[str] = []
        self.permission_handler: PermissionHandler | None = None
        self.request_permission = request_permission
        self.permission_result: PermissionConfirmation | None = None
        self.provider_updates: list[ProviderUpdate] = []
        self.resumed_session_ids: list[str] = []
        self.session_summaries: tuple[SessionSummary, ...] = ()
        self.compact_calls = 0
        self.todos: tuple[TodoItem, ...] = ()
        self.todo_update: tuple[TodoItem, ...] | None = None

    async def submit(self, prompt: str) -> TurnOutcome:
        self.prompts.append(prompt)
        return TurnSucceeded("**model response**", 1, 10, 2)

    async def stream(self, prompt: str) -> AsyncIterator[TurnEvent]:
        self.prompts.append(prompt)
        if self.request_permission:
            assert self.permission_handler is not None
            use = ToolUsePresentation("Write", "a.txt", "Writing a.txt")
            yield ToolStarted("tool-1", use)
            self.permission_result = await self.permission_handler(
                PermissionRequest("Write", {"path": "a.txt"}, "Allow this write?", use)
            )
            yield ToolFinished(
                "tool-1",
                not self.permission_result.allowed,
                ToolResultPresentation(
                    summary=(
                        "Wrote 4 bytes to a.txt"
                        if self.permission_result.allowed
                        else "Permission denied"
                    )
                ),
            )
        if self.todo_update is not None:
            self.todos = self.todo_update
            yield TodoListUpdated(self.todos)
        yield TextDelta("**model ")
        yield TextDelta("response**")
        yield TurnCompleted(TurnSucceeded("**model response**", 1, 10, 2))

    def status(self) -> RuntimeStatus:
        return RuntimeStatus(
            session_id="session-id",
            cwd="/workspace",
            provider_id="anthropic",
            base_url=None,
            model="test-model",
            permission_mode="default",
            credential_source="stored",
            working_message_count=len(self.prompts) * 2,
            todos=self.todos,
        )

    def context_status(self) -> ContextStatus:
        return ContextStatus(
            estimated_input_tokens=100,
            reserved_output_tokens=20,
            estimated_total_tokens=120,
            message_chars=200,
            system_chars=50,
            tool_schema_chars=75,
            message_limit_chars=1000,
            working_message_count=2,
            replacement_count=1,
            compact_count=self.compact_calls,
        )

    async def compact(self) -> ContextStatus:
        self.compact_calls += 1
        return self.context_status()

    def set_permission_handler(self, handler: PermissionHandler) -> None:
        self.permission_handler = handler

    def providers(self) -> tuple[ProviderView, ...]:
        return (
            ProviderView(
                id="anthropic",
                protocol=ProviderProtocol.ANTHROPIC_MESSAGES,
                model="test-model",
                base_url=None,
                active=True,
                has_stored_key=True,
            ),
        )

    async def configure_provider(self, update: ProviderUpdate) -> RuntimeStatus:
        self.provider_updates.append(update)
        return self.status()

    async def list_sessions(self) -> tuple[SessionSummary, ...]:
        return self.session_summaries

    async def resume_session(self, session_id: str) -> ResumedSession:
        self.resumed_session_ids.append(session_id)
        return ResumedSession(
            status=self.status(),
            history=(
                HistoryUserMessage("old prompt"),
                HistoryAssistantMessage("old response"),
            ),
        )


class MaxTurnsRuntime(FakeRuntime):
    async def stream(self, prompt: str) -> AsyncIterator[TurnEvent]:
        self.prompts.append(prompt)
        yield TurnLimitReached(MaxTurnsReached(3, 3, 30, 6))


def test_slash_registry_filters_candidates_by_prefix() -> None:
    matches = SlashCommandRegistry.default().matching("/st")

    assert [command.name for command in matches] == ["status"]


def test_provider_slash_command_requests_manager_screen() -> None:
    outcome = SlashCommandRegistry.default().dispatch(
        "/provider", status=FakeRuntime().status()
    )

    assert outcome is not None
    assert outcome.open_provider_manager is True


def test_resume_slash_command_requests_session_picker() -> None:
    outcome = SlashCommandRegistry.default().dispatch(
        "/resume", status=FakeRuntime().status()
    )

    assert outcome is not None
    assert outcome.open_session_picker is True


def test_context_and_compact_commands_request_runtime_actions() -> None:
    registry = SlashCommandRegistry.default()
    status = FakeRuntime().status()

    context = registry.dispatch("/context", status=status)
    compact = registry.dispatch("/compact", status=status)

    assert context is not None and context.show_context is True
    assert compact is not None and compact.compact_context is True


@pytest.mark.asyncio
async def test_tui_dispatches_selected_slash_command_locally() -> None:
    runtime = FakeRuntime()
    app = NanoCodeApp(runtime)

    async with app.run_test(size=(100, 32)) as pilot:
        prompt = app.query_one("#prompt", Input)
        prompt.value = "/st"
        await pilot.pause()

        palette = app.query_one("#command-palette", OptionList)
        assert palette.display is True
        assert palette.get_option_at_index(0).id == "status"

        await pilot.press("enter")
        await pilot.pause()

        assert runtime.prompts == []
        assert len(app.query(SystemMessage)) == 1


@pytest.mark.asyncio
async def test_context_and_compact_render_runtime_diagnostics() -> None:
    runtime = FakeRuntime()
    app = NanoCodeApp(runtime)

    async with app.run_test(size=(100, 32)) as pilot:
        app.query_one("#prompt", Input).value = "/context"
        await pilot.press("enter")
        await pilot.pause()
        assert "Estimated input: 100 tokens" in str(
            app.query(SystemMessage)[-1].render()
        )

        app.query_one("#prompt", Input).value = "/compact"
        await pilot.press("enter")
        await pilot.pause()

        assert runtime.compact_calls == 1
        assert "Conversation compacted" in str(app.query(SystemMessage)[-1].render())


@pytest.mark.asyncio
async def test_provider_slash_command_opens_profile_editor() -> None:
    app = NanoCodeApp(FakeRuntime())

    async with app.run_test(size=(110, 36)) as pilot:
        app.query_one("#prompt", Input).value = "/provider"
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, ProviderScreen)
        assert app.screen.query_one("#provider-key", Input).password is True
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_resume_picker_selects_session_and_replaces_conversation() -> None:
    runtime = FakeRuntime()
    session_id = "12345678-1234-1234-1234-123456789abc"
    runtime.session_summaries = (
        SessionSummary(
            session_id=session_id,
            title="Fix session resume",
            updated_at=datetime.now(UTC),
        ),
    )
    app = NanoCodeApp(runtime)

    async with app.run_test(size=(100, 36)) as pilot:
        app.query_one("#prompt", Input).value = "/resume"
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, ResumeScreen)
        await pilot.press("enter")
        await pilot.pause()

        assert runtime.resumed_session_ids == [session_id]
        assert [message.prompt for message in app.query(UserMessage)] == ["old prompt"]
        assert [message.source for message in app.query(AssistantMessage)] == [
            "old response"
        ]


@pytest.mark.asyncio
async def test_provider_editor_submits_password_key_to_runtime() -> None:
    runtime = FakeRuntime()
    app = NanoCodeApp(runtime)

    async with app.run_test(size=(110, 36)) as pilot:
        app.query_one("#prompt", Input).value = "/provider"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.click("#provider-new")
        app.screen.query_one("#provider-id", Input).value = "gateway"
        app.screen.query_one(
            "#provider-url", Input
        ).value = "https://gateway.example/api"
        app.screen.query_one("#provider-model", Input).value = "gateway-model"
        app.screen.query_one("#provider-key", Input).value = "secret-key"

        await pilot.click("#provider-save")
        await pilot.pause()

        assert runtime.provider_updates == [
            ProviderUpdate(
                id="gateway",
                model="gateway-model",
                base_url="https://gateway.example/api",
                api_key="secret-key",
            )
        ]


@pytest.mark.asyncio
async def test_permission_request_uses_inline_panel_and_returns_explicit_choice() -> (
    None
):
    app = NanoCodeApp(FakeRuntime())

    async with app.run_test(size=(100, 32)) as pilot:
        permission = asyncio.create_task(
            app._ask_permission(
                PermissionRequest(
                    "Write",
                    {"path": "a.txt"},
                    "Allow this write?",
                    ToolUsePresentation("Write", "a.txt", "Writing a.txt"),
                )
            )
        )
        await pilot.pause()

        panel = app.query_one(PermissionPanel)
        assert panel.display is True
        assert app.query_one("#prompt", Input).display is False
        await pilot.press("1")

        assert await permission == PermissionConfirmation(True)


@pytest.mark.asyncio
async def test_permission_feedback_is_returned_to_runtime() -> None:
    app = NanoCodeApp(FakeRuntime())

    async with app.run_test(size=(100, 32)) as pilot:
        permission = asyncio.create_task(
            app._ask_permission(
                PermissionRequest(
                    "Bash",
                    {"command": "git push"},
                    "Run command?",
                    ToolUsePresentation("Bash", "git push", "Running command"),
                )
            )
        )
        await pilot.pause()
        await pilot.press("3")

        feedback = app.query_one("#permission-feedback", Input)
        assert feedback.display is True
        feedback.value = "Do not push; only show the diff."
        await pilot.press("enter")

        assert await permission == PermissionConfirmation(
            False, "Do not push; only show the diff."
        )


@pytest.mark.asyncio
async def test_tui_streams_markdown_and_updates_tool_result_in_place() -> None:
    runtime = FakeRuntime(request_permission=True)
    app = NanoCodeApp(runtime)

    async with app.run_test(size=(100, 36)) as pilot:
        app.query_one("#prompt", Input).value = "write it"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("1")
        await pilot.pause()

        tool = app.query_one(ToolCallMessage)
        assistant = app.query_one(AssistantMessage)
        assert tool.result == ToolResultPresentation(summary="Wrote 4 bytes to a.txt")
        assert assistant.source == "**model response**"
        assert runtime.permission_result == PermissionConfirmation(True)


@pytest.mark.asyncio
async def test_tui_renders_structured_max_turns_terminal_outcome() -> None:
    app = NanoCodeApp(MaxTurnsRuntime())

    async with app.run_test(size=(100, 32)) as pilot:
        app.query_one("#prompt", Input).value = "keep working"
        await pilot.press("enter")
        await pilot.pause()

        assert "Reached max turns (3)" in str(app.query(SystemMessage)[-1].render())
        assert app.query_one("#prompt", Input).disabled is False


@pytest.mark.asyncio
async def test_tui_updates_and_toggles_todo_panel_during_turn() -> None:
    runtime = FakeRuntime()
    runtime.todo_update = (
        TodoItem("Inspect implementation", "completed", "Inspecting implementation"),
        TodoItem("Run tests", "in_progress", "Running tests"),
        TodoItem("Write docs", "pending", "Writing docs"),
    )
    app = NanoCodeApp(runtime)

    async with app.run_test(size=(100, 36)) as pilot:
        panel = app.query_one(TodoPanel)
        assert panel.display is False

        app.query_one("#prompt", Input).value = "continue"
        await pilot.press("enter")
        await pilot.pause()

        assert panel.display is True
        assert panel.expanded is True
        assert panel.todos == runtime.todo_update
        console = Console(width=100, color_system=None)
        with console.capture() as capture:
            console.print(panel.render())
        assert "✓ Inspect implementation" in capture.get()
        activity = app.query_one(ActivityBar)
        assert "Running tests" in str(activity.query_one("Label").render())

        await pilot.press("ctrl+t")
        assert panel.expanded is False

        runtime.todo_update = (TodoItem("Write docs", "in_progress", "Writing docs"),)
        app.query_one("#prompt", Input).value = "continue again"
        await pilot.press("enter")
        await pilot.pause()

        assert panel.todos == runtime.todo_update
        assert panel.expanded is False


@pytest.mark.asyncio
async def test_tui_hides_panel_when_todos_are_cleared() -> None:
    runtime = FakeRuntime()
    runtime.todos = (TodoItem("Run tests", "in_progress", "Running tests"),)
    runtime.todo_update = ()
    app = NanoCodeApp(runtime)

    async with app.run_test(size=(100, 36)) as pilot:
        panel = app.query_one(TodoPanel)
        assert panel.display is True

        app.query_one("#prompt", Input).value = "finish"
        await pilot.press("enter")
        await pilot.pause()

        assert panel.todos == ()
        assert panel.display is False
