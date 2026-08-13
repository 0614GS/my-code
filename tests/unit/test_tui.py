import asyncio
from collections.abc import AsyncIterator

import pytest
from textual.widgets import Input, OptionList

from nano_code.permissions import PermissionConfirmation
from nano_code.providers.manager import ProviderUpdate, ProviderView
from nano_code.providers.profiles import ProviderProtocol
from nano_code.tui import NanoCodeApp, PermissionHandler, ProviderScreen, RuntimeStatus
from nano_code.tui.commands import SlashCommandRegistry
from nano_code.tui.contracts import (
    PermissionRequest,
    TextDelta,
    ToolFinished,
    ToolStarted,
    TurnCompleted,
    TurnEvent,
    TurnResult,
)
from nano_code.tui.widgets import (
    AssistantMessage,
    PermissionPanel,
    SystemMessage,
    ToolCallMessage,
)


class FakeRuntime:
    def __init__(self, *, request_permission: bool = False) -> None:
        self.prompts: list[str] = []
        self.permission_handler: PermissionHandler | None = None
        self.request_permission = request_permission
        self.permission_result: PermissionConfirmation | None = None
        self.provider_updates: list[ProviderUpdate] = []

    async def submit(self, prompt: str) -> TurnResult:
        self.prompts.append(prompt)
        return TurnResult("**model response**", 1, 10, 2)

    async def stream(self, prompt: str) -> AsyncIterator[TurnEvent]:
        self.prompts.append(prompt)
        if self.request_permission:
            assert self.permission_handler is not None
            yield ToolStarted("tool-1", "Write", {"path": "a.txt"})
            self.permission_result = await self.permission_handler(
                PermissionRequest("Write", {"path": "a.txt"}, "Allow this write?")
            )
            yield ToolFinished(
                "tool-1",
                "Write",
                "Wrote 4 bytes to a.txt"
                if self.permission_result.allowed
                else "Permission denied",
                not self.permission_result.allowed,
            )
        yield TextDelta("**model ")
        yield TextDelta("response**")
        yield TurnCompleted(TurnResult("**model response**", 1, 10, 2))

    def status(self) -> RuntimeStatus:
        return RuntimeStatus(
            session_id="session-id",
            cwd="/workspace",
            provider_id="anthropic",
            base_url=None,
            model="test-model",
            permission_mode="default",
            credential_source="stored",
            message_count=len(self.prompts) * 2,
        )

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


def test_slash_registry_filters_candidates_by_prefix() -> None:
    matches = SlashCommandRegistry.default().matching("/st")

    assert [command.name for command in matches] == ["status"]


def test_provider_slash_command_requests_manager_screen() -> None:
    outcome = SlashCommandRegistry.default().dispatch(
        "/provider", status=FakeRuntime().status()
    )

    assert outcome is not None
    assert outcome.open_provider_manager is True


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
                PermissionRequest("Write", {"path": "a.txt"}, "Allow this write?")
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
                PermissionRequest("Bash", {"command": "git push"}, "Run command?")
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
        assert tool.result == "Wrote 4 bytes to a.txt"
        assert assistant.source == "**model response**"
        assert runtime.permission_result == PermissionConfirmation(True)
