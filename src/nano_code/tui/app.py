"""负责渲染 nano-code、但不持有运行时的 Textual 应用。"""

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.events import Key
from textual.widgets import Input, OptionList
from textual.widgets.option_list import Option

from nano_code.permissions import PermissionConfirmation
from nano_code.tui.commands import SlashCommandRegistry
from nano_code.tui.contracts import (
    ChatRuntime,
    ContextStatus,
    HistoryAssistantMessage,
    HistorySystemMessage,
    HistoryToolCall,
    HistoryUserMessage,
    PermissionRequest,
    TextDelta,
    TodoListUpdated,
    ToolFinished,
    ToolStarted,
    TurnCompleted,
    TurnLimitReached,
)
from nano_code.tui.provider_screen import ProviderScreen
from nano_code.tui.resume_screen import ResumeScreen
from nano_code.tui.widgets import (
    ActivityBar,
    AssistantMessage,
    PermissionPanel,
    StatusBar,
    SystemMessage,
    TodoPanel,
    ToolCallMessage,
    UserMessage,
    WelcomePanel,
)


class NanoCodeApp(App[None]):
    """类似 Claude Code REPL 外壳的组件化终端 UI。"""

    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [Binding("ctrl+t", "toggle_todos", "Toggle todos", show=False)]
    CSS = """
    Screen {
        background: #171717;
        color: #e8e4df;
    }

    #conversation {
        height: 1fr;
        padding: 1 2 0 2;
        scrollbar-color: #6f625a;
        scrollbar-background: #171717;
    }

    TodoPanel {
        display: none;
        width: 100%;
        height: auto;
        max-height: 10;
        overflow-y: auto;
        padding: 0 3;
        border-top: solid #3a3532;
        background: #1e1c1b;
    }

    WelcomePanel {
        width: 100%;
        height: auto;
        min-height: 7;
        padding: 1 2;
        margin-bottom: 1;
        border: round #d97757;
        content-align: center middle;
        text-align: center;
        background: #1e1c1b;
    }

    .message {
        width: 100%;
        height: auto;
        margin-bottom: 1;
    }

    UserMessage {
        padding: 0 1;
        border-left: thick #d97757;
        background: #211f1e;
    }

    AssistantMessage {
        padding: 0 1 0 2;
        background: #171717;
    }

    SystemMessage {
        padding: 0 2;
        color: #a9a19a;
    }

    SystemMessage.error {
        color: #ff7b72;
        border-left: thick #ff7b72;
    }

    #composer {
        height: auto;
        max-height: 14;
        padding: 0 2 1 2;
        background: #171717;
    }

    #command-palette {
        display: none;
        height: auto;
        max-height: 7;
        margin: 0 1;
        border: round #6f625a;
        background: #211f1e;
        color: #e8e4df;
    }

    #command-palette > .option-list--option-highlighted {
        background: #56372d;
        color: #fff8f2;
    }

    ActivityBar {
        display: none;
        height: 1;
        margin: 0 1;
        color: #d97757;
    }

    ActivityBar LoadingIndicator {
        width: 4;
        height: 1;
        color: #d97757;
    }

    #prompt {
        height: 3;
        border: round #6f625a;
        background: #211f1e;
        color: #f3efeb;
        padding: 0 1;
    }

    #prompt:focus {
        border: round #d97757;
    }

    StatusBar {
        height: 1;
        margin: 0 1;
        color: #8f8882;
    }

    ProviderScreen {
        align: center middle;
        background: #000000 65%;
    }

    ResumeScreen {
        align: center middle;
        background: #000000 65%;
    }

    #resume-dialog {
        width: 88%;
        max-width: 100;
        height: 70%;
        min-height: 12;
        padding: 1 2;
        border: round #d97757;
        background: #211f1e;
    }

    #resume-title {
        height: 1;
        color: #ffb38a;
        text-style: bold;
    }

    #resume-description, #resume-hint {
        height: 1;
        color: #8f8882;
    }

    #resume-list {
        height: 1fr;
        margin: 1 0;
        background: #211f1e;
    }

    #resume-list > .option-list--option {
        height: 3;
    }

    #resume-list > .option-list--option-highlighted {
        background: #56372d;
        color: #fff8f2;
    }

    #provider-dialog {
        width: 92%;
        max-width: 100;
        height: 30;
        padding: 1 2;
        border: round #d97757;
        background: #211f1e;
    }

    #provider-title {
        height: 1;
        color: #ffb38a;
        text-style: bold;
    }

    #provider-description, #provider-key-status, #provider-error {
        height: 1;
        color: #8f8882;
    }

    #provider-error {
        color: #ff7b72;
    }

    #provider-content {
        height: 1fr;
        margin-top: 1;
    }

    #provider-list {
        width: 30%;
        height: 100%;
        border: round #6f625a;
        margin-right: 2;
    }

    #provider-form {
        width: 1fr;
        height: 100%;
    }

    #provider-form Label {
        height: 1;
    }

    #provider-form Input {
        height: 3;
        margin-bottom: 1;
    }

    #provider-actions {
        height: 3;
        align-horizontal: right;
    }

    #provider-actions Button {
        margin-left: 1;
    }

    PermissionPanel {
        display: none;
        width: 100%;
        height: auto;
        max-height: 18;
        padding: 0 1;
        border-top: round #d97757;
        background: #211f1e;
    }

    #permission-title {
        height: 1;
        color: #ffb38a;
        text-style: bold;
    }

    #permission-detail {
        height: auto;
        max-height: 5;
        margin: 0 1;
    }

    #permission-question {
        height: 1;
        margin: 0 1;
        text-style: bold;
    }

    #permission-options {
        height: 3;
        margin: 0 1;
        background: #211f1e;
    }

    #permission-options > .option-list--option-highlighted {
        background: #56372d;
        color: #fff8f2;
    }

    #permission-feedback {
        display: none;
        height: 3;
        margin: 0 1;
        border: round #d97757;
    }

    #permission-hint {
        height: 1;
        margin: 0 1;
        color: #8f8882;
    }

    ToolCallMessage {
        padding: 0 1 0 2;
        color: #a9a19a;
    }

    ToolCallMessage.error {
        color: #ff7b72;
    }
    """

    def __init__(
        self,
        runtime: ChatRuntime,
        *,
        commands: SlashCommandRegistry | None = None,
    ) -> None:
        super().__init__(ansi_color=True)
        self.runtime = runtime
        self.commands = commands or SlashCommandRegistry.default()
        self._busy = False
        self.runtime.set_permission_handler(self._ask_permission)

    def compose(self) -> ComposeResult:
        status = self.runtime.status()
        with VerticalScroll(id="conversation"):
            yield WelcomePanel(status)
        yield TodoPanel(status.todos)
        with Vertical(id="composer"):
            yield OptionList(id="command-palette", compact=True)
            yield ActivityBar(status.todos)
            yield PermissionPanel()
            yield Input(
                placeholder="Ask nano-code anything, or type / for commands",
                id="prompt",
            )
            yield StatusBar(status)

    def on_mount(self) -> None:
        self.query_one("#prompt", Input).focus()

    def action_toggle_todos(self) -> None:
        self.query_one(TodoPanel).toggle()

    @on(Input.Changed, "#prompt")
    def update_slash_suggestions(self, event: Input.Changed) -> None:
        palette = self.query_one("#command-palette", OptionList)
        matches = self.commands.matching(event.value)
        palette.set_options(
            Option(
                f"[bold #ffb38a]/{command.name}[/]  [dim]{command.description}[/]",
                id=command.name,
            )
            for command in matches
        )
        palette.display = bool(matches)
        if matches:
            palette.highlighted = 0

    async def on_key(self, event: Key) -> None:
        prompt = self.query_one("#prompt", Input)
        palette = self.query_one("#command-palette", OptionList)
        if not prompt.has_focus or not palette.display or palette.option_count == 0:
            return
        if event.key == "down":
            palette.action_cursor_down()
        elif event.key == "up":
            palette.action_cursor_up()
        elif event.key == "tab":
            command_name = _highlighted_command(palette)
            if command_name is not None:
                prompt.value = f"/{command_name} "
                prompt.cursor_position = len(prompt.value)
            event.prevent_default()
            event.stop()

    @on(OptionList.OptionSelected, "#command-palette")
    async def select_slash_command(self, event: OptionList.OptionSelected) -> None:
        if event.option_id is not None:
            await self._process_line(f"/{event.option_id}")

    @on(Input.Submitted, "#prompt")
    async def submit_prompt(self, event: Input.Submitted) -> None:
        if self._busy:
            return
        palette = self.query_one("#command-palette", OptionList)
        line = event.value
        if palette.display and palette.option_count:
            command_name = _highlighted_command(palette)
            if command_name is not None:
                line = f"/{command_name}"
        await self._process_line(line)

    async def _process_line(self, line: str) -> None:
        prompt = self.query_one("#prompt", Input)
        palette = self.query_one("#command-palette", OptionList)
        prompt.value = ""
        palette.display = False
        if not line.strip():
            return

        outcome = self.commands.dispatch(line, status=self.runtime.status())
        if outcome is not None:
            if outcome.clear_screen:
                await self._clear_messages()
            if outcome.message:
                await self._mount_message(SystemMessage(outcome.message))
            if outcome.open_provider_manager:
                self._manage_providers()
            if outcome.open_session_picker:
                self._resume_session()
            if outcome.show_context:
                await self._mount_message(
                    SystemMessage(_render_context_status(self.runtime.context_status()))
                )
            if outcome.compact_context:
                self._compact_context()
            if outcome.should_exit:
                self.exit()
            return
        self._run_agent_turn(line)

    @work(exclusive=True, group="agent-turn")
    async def _run_agent_turn(self, prompt_text: str) -> None:
        self._busy = True
        prompt = self.query_one("#prompt", Input)
        activity = self.query_one(ActivityBar)
        prompt.disabled = True
        activity.display = True
        await self._mount_message(UserMessage(prompt_text))
        assistant: AssistantMessage | None = None
        tools: dict[str, ToolCallMessage] = {}
        completed = False
        try:
            async for event in self.runtime.stream(prompt_text):
                if isinstance(event, TextDelta):
                    if assistant is None:
                        assistant = AssistantMessage("")
                        await self._mount_message(assistant)
                    await assistant.append_delta(event.text)
                    self._scroll_to_end()
                elif isinstance(event, ToolStarted):
                    if assistant is not None:
                        await assistant.finish_stream()
                        assistant = None
                    tool_message = ToolCallMessage(
                        event.tool_use_id,
                        event.presentation,
                    )
                    tools[event.tool_use_id] = tool_message
                    await self._mount_message(tool_message)
                elif isinstance(event, ToolFinished):
                    finished_message = tools.get(event.tool_use_id)
                    if finished_message is not None:
                        finished_message.finish(
                            event.presentation, is_error=event.is_error
                        )
                        self._scroll_to_end()
                elif isinstance(event, TodoListUpdated):
                    self.query_one(TodoPanel).set_todos(event.todos)
                    activity.set_todos(event.todos)
                elif isinstance(event, TurnCompleted):
                    completed = True
                elif isinstance(event, TurnLimitReached):
                    completed = True
                    await self._mount_message(
                        SystemMessage(
                            f"Error: Reached max turns ({event.result.max_turns})",
                            error=True,
                        )
                    )
        except Exception as error:
            await self._mount_message(SystemMessage(f"Error: {error}", error=True))
        finally:
            if assistant is not None:
                await assistant.finish_stream()
            if completed is False and not tools and assistant is None:
                await self._mount_message(AssistantMessage("<no text response>"))
            activity.display = False
            prompt.disabled = False
            prompt.focus()
            self._busy = False
            status = self.runtime.status()
            self.query_one(StatusBar).set_status(status)
            self.query_one(TodoPanel).set_todos(status.todos)
            activity.set_todos(status.todos)

    @work(exclusive=True, group="provider-dialog")
    async def _manage_providers(self) -> None:
        try:
            update = await self.push_screen_wait(
                ProviderScreen(self.runtime.providers())
            )
            if update is None:
                return
            status = await self.runtime.configure_provider(update)
        except Exception as error:
            await self._mount_message(
                SystemMessage(f"Provider configuration failed: {error}", error=True)
            )
        else:
            self.query_one(StatusBar).set_status(status)
            self.query_one(TodoPanel).set_todos(status.todos)
            self.query_one(ActivityBar).set_todos(status.todos)
            welcome = self.query_one(WelcomePanel)
            welcome.status = status
            welcome.refresh()
            endpoint = status.base_url or "Anthropic SDK default"
            await self._mount_message(
                SystemMessage(
                    f"Using provider {status.provider_id!r} · "
                    f"{status.model} · {endpoint}"
                )
            )
        finally:
            self.query_one("#prompt", Input).focus()

    @work(exclusive=True, group="resume-dialog")
    async def _resume_session(self) -> None:
        prompt = self.query_one("#prompt", Input)
        prompt.disabled = True
        try:
            sessions = await self.runtime.list_sessions()
            if not sessions:
                await self._mount_message(
                    SystemMessage("No conversations found to resume.")
                )
                return
            session_id = await self.push_screen_wait(ResumeScreen(sessions))
            if session_id is None:
                return
            resumed = await self.runtime.resume_session(session_id)
            await self._render_history(resumed.history)
            self.query_one(StatusBar).set_status(resumed.status)
            self.query_one(TodoPanel).set_todos(
                resumed.status.todos, reset_session=True
            )
            self.query_one(ActivityBar).set_todos(resumed.status.todos)
            welcome = self.query_one(WelcomePanel)
            welcome.status = resumed.status
            welcome.refresh()
        except Exception as error:
            await self._mount_message(
                SystemMessage(f"Failed to resume conversation: {error}", error=True)
            )
        finally:
            prompt.disabled = False
            prompt.focus()

    @work(exclusive=True, group="agent-turn")
    async def _compact_context(self) -> None:
        prompt = self.query_one("#prompt", Input)
        activity = self.query_one(ActivityBar)
        self._busy = True
        prompt.disabled = True
        activity.display = True
        try:
            status = await self.runtime.compact()
            await self._mount_message(
                SystemMessage(
                    "Conversation compacted.\n" + _render_context_status(status)
                )
            )
        except Exception as error:
            await self._mount_message(
                SystemMessage(f"Compaction failed: {error}", error=True)
            )
        finally:
            activity.display = False
            prompt.disabled = False
            prompt.focus()
            self._busy = False
            runtime_status = self.runtime.status()
            self.query_one(StatusBar).set_status(runtime_status)
            self.query_one(TodoPanel).set_todos(runtime_status.todos)
            activity.set_todos(runtime_status.todos)

    async def _render_history(
        self,
        history: tuple[
            HistoryUserMessage
            | HistoryAssistantMessage
            | HistorySystemMessage
            | HistoryToolCall,
            ...,
        ],
    ) -> None:
        await self._clear_messages()
        for entry in history:
            if isinstance(entry, HistoryUserMessage):
                await self._mount_message(UserMessage(entry.text))
            elif isinstance(entry, HistoryAssistantMessage):
                await self._mount_message(AssistantMessage(entry.text))
            elif isinstance(entry, HistorySystemMessage):
                await self._mount_message(SystemMessage(entry.text))
            elif isinstance(entry, HistoryToolCall):
                tool = ToolCallMessage(entry.tool_use_id, entry.use)
                tool.finish(entry.result, is_error=entry.is_error)
                await self._mount_message(tool)

    async def _mount_message(
        self,
        message: UserMessage | AssistantMessage | SystemMessage | ToolCallMessage,
    ) -> None:
        conversation = self.query_one("#conversation", VerticalScroll)
        await conversation.mount(message)
        conversation.scroll_end(animate=False)

    def _scroll_to_end(self) -> None:
        self.query_one("#conversation", VerticalScroll).scroll_end(animate=False)

    async def _clear_messages(self) -> None:
        for message in list(self.query(".message")):
            await message.remove()

    async def _ask_permission(
        self, request: PermissionRequest
    ) -> PermissionConfirmation:
        prompt = self.query_one("#prompt", Input)
        activity = self.query_one(ActivityBar)
        panel = self.query_one(PermissionPanel)
        prompt.display = False
        activity.display = False
        try:
            return await panel.ask(request)
        finally:
            prompt.display = True
            activity.display = True


class NanoCodeTui:
    """保留精简启动器，使 CLI 不依赖 Textual 的 App API。"""

    def __init__(self, runtime: ChatRuntime) -> None:
        self.app = NanoCodeApp(runtime)

    async def run(self) -> None:
        await self.app.run_async()


def _highlighted_command(palette: OptionList) -> str | None:
    highlighted = palette.highlighted
    if highlighted is None:
        return None
    return palette.get_option_at_index(highlighted).id


def _render_context_status(status: ContextStatus) -> str:
    return "\n".join(
        (
            f"Estimated input: {status.estimated_input_tokens} tokens",
            f"Reserved output: {status.reserved_output_tokens} tokens",
            f"Estimated total: {status.estimated_total_tokens} tokens",
            (
                "Characters: "
                f"messages {status.message_chars} · system {status.system_chars} · "
                f"user context {status.user_context_chars} · "
                f"attachments {status.attachment_chars} · "
                f"tools {status.tool_schema_chars}"
            ),
            (
                f"Working messages: {status.working_message_count} · "
                f"microcompacts: {status.replacement_count} · "
                f"compacts: {status.compact_count}"
            ),
        )
    )
