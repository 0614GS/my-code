"""Native, non-full-screen prompt_toolkit host with Rich scrollback output."""

from __future__ import annotations

import asyncio
from typing import Any, cast

from prompt_toolkit.application import Application, run_in_terminal
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import (
    AnyFormattedText,
    FormattedText,
    to_formatted_text,
)
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import Input
from prompt_toolkit.layout import BufferControl
from prompt_toolkit.layout.processors import (
    BeforeInput,
    ConditionalProcessor,
    PasswordProcessor,
)
from prompt_toolkit.output import Output
from rich.console import Console, RenderableType

from my_code.chat.events import (
    BackgroundInvocationFinished,
    BackgroundInvocationStarted,
    TurnEvent,
)
from my_code.chat.history import (
    HistoryEntry,
    HistoryReasoning,
    HistoryText,
    HistoryToolCall,
)
from my_code.chat.permissions import PermissionRequest
from my_code.chat.service import ChatService
from my_code.chat.status import ContextStatus, RuntimeStatus
from my_code.chat.views import SubagentTaskView
from my_code.features.file_mentions.models import PathSuggestion
from my_code.permissions.models import (
    PermissionBehavior,
    PermissionConfirmation,
    PermissionUpdate,
    PermissionUpdateDestination,
)
from my_code.permissions.rules import validate_bash_rule_content
from my_code.permissions.updates import permission_rule_for_destination
from my_code.providers.manager import ProviderView
from my_code.sessions.catalog import SessionSummary
from my_code.tools.presentation import ToolUsePresentation
from my_code.tui.commands import CommandOutcome, SlashCommandRegistry
from my_code.tui.completion import mention_at_cursor
from my_code.tui.composer import ComposerCompleter, SlashMenuState
from my_code.tui.key_bindings import build_key_bindings
from my_code.tui.layout import build_terminal_layout
from my_code.tui.panels import (
    permission_panel,
    provider_actions_panel,
    provider_form_panel,
    provider_models_panel,
    provider_remove_credential_panel,
    provider_review_panel,
    provider_select_panel,
    resume_panel,
)
from my_code.tui.presentation import (
    format_context_usage,
    render_agent_view,
    render_context_status,
    render_mcp,
    render_skills,
    render_tasks,
    render_tools,
    render_usage,
)
from my_code.tui.provider_screen import (
    PROVIDER_ADVANCED_FIELDS,
    PROVIDER_CORE_FIELDS,
    ProviderForm,
)
from my_code.tui.terminal import terminal_supports_true_color
from my_code.tui.theme import TuiTheme
from my_code.tui.turns import TurnFlowMixin
from my_code.tui.widgets import (
    assistant_message,
    reasoning_message,
    status_line,
    streaming_assistant_message,
    system_message,
    todo_text,
    tool_message,
    user_message,
    welcome,
)


class MyCodeApp(TurnFlowMixin):
    """Inline terminal application; canonical state stays in ChatService."""

    def __init__(
        self,
        runtime: ChatService,
        *,
        commands: SlashCommandRegistry | None = None,
        input: Input | None = None,
        output: Output | None = None,
        console: Console | None = None,
    ) -> None:
        self.runtime = runtime
        self.commands = commands or SlashCommandRegistry.default()
        self.theme = TuiTheme.detect()
        self.console = console or Console(
            color_system="truecolor" if terminal_supports_true_color() else "auto"
        )
        self._busy = False
        self._running = False
        self._activity = ""
        self._stream_text = ""
        self._reasoning_parts: list[str] = []
        self._todos = ()
        self._todos_expanded = True
        self._status: RuntimeStatus | None = None
        self._context_status: ContextStatus | None = None
        self._status_warning = ""
        self._agents: tuple[SubagentTaskView, ...] = ()
        self._agent_scroll = 0
        self._panel: str | None = None
        self._panel_index = 0
        self._sessions: tuple[SessionSummary, ...] = ()
        self._providers: tuple[ProviderView, ...] = ()
        self._provider_form: ProviderForm | None = None
        self._provider_selected_index = 0
        self._provider_fields = PROVIDER_CORE_FIELDS
        self._provider_field = 0
        self._provider_models: tuple[str, ...] = ()
        self._permission_request: PermissionRequest | None = None
        self._permission_future: asyncio.Future[PermissionConfirmation] | None = None
        self._permission_mode = "select"
        self._saved_draft = ""
        self._path_suggestions: tuple[PathSuggestion, ...] = ()
        self._mention_span: tuple[int, int] | None = None
        self._suggestion_revision = 0
        self._foreground_task: asyncio.Task[None] | None = None
        self._background_tools: dict[str, ToolUsePresentation] = {}
        self._tasks: set[asyncio.Task[object]] = set()
        self._history = InMemoryHistory()
        self._slash_menu = SlashMenuState()
        self.buffer = Buffer(
            multiline=True,
            history=self._history,
            completer=ComposerCompleter(self),
            complete_while_typing=Condition(self._complete_while_typing),
            read_only=Condition(lambda: self._composer_read_only()),
        )
        self.buffer.on_text_changed += self._on_text_changed
        prompt = BeforeInput(FormattedText([("class:prompt", "› ")]))
        password = ConditionalProcessor(
            PasswordProcessor(), Condition(lambda: self._provider_password_field())
        )
        self.input_control = BufferControl(
            buffer=self.buffer, input_processors=[prompt, password]
        )
        self.key_bindings = build_key_bindings(self)
        terminal_layout = build_terminal_layout(
            input_control=self.input_control,
            key_bindings=self.key_bindings,
            dynamic_text=self._dynamic_text,
            todo_text=self._todo_display,
            has_todos=lambda: bool(self._todos),
            status_text=self._status_text,
            slash_menu_text=self._slash_menu_text,
            has_slash_menu=self._slash_active,
            input=input,
            output=output,
            theme=self.theme,
        )
        self.application: Application[None] = terminal_layout.application
        self.body = terminal_layout.body
        self.completions_menu = terminal_layout.completions_menu
        self.slash_menu = terminal_layout.slash_menu
        self.runtime.set_permission_handler(self._ask_permission)

    async def run_async(self) -> None:
        view = await self.runtime.initialize()
        self._status = view.status
        self._context_status = self.runtime.context_status()
        self._todos = view.status.todos
        await self._write(welcome(view.status, self.theme))
        if view.history:
            await self._render_history(view.history)
            for entry in view.history:
                if isinstance(entry, HistoryText) and entry.role == "user":
                    self._history.append_string(entry.text)
        self._running = True

        def start_background() -> None:
            self._spawn(self._watch_background_notifications())
            self._spawn(self._watch_subagent_activity())

        try:
            await self.application.run_async(pre_run=start_background)
        finally:
            self._running = False
            for task in tuple(self._tasks):
                task.cancel()
            if self._tasks:
                await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    def _spawn(self, coroutine: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine)
        self._tasks.add(cast(asyncio.Task[object], task))
        task.add_done_callback(self._tasks.discard)
        return task

    async def _write(self, renderable: RenderableType, *, clear: bool = False) -> None:
        def render() -> None:
            if clear:
                self.console.clear()
            self.console.print(renderable)

        if self._running:
            await run_in_terminal(render)
        else:
            render()

    def _invalidate(self) -> None:
        self.application.invalidate()

    def _composer_read_only(self) -> bool:
        if self._panel == "permission":
            return self._permission_mode == "select"
        if self._panel in {
            "resume",
            "provider_select",
            "provider_actions",
            "provider_remove_credential",
            "provider_review",
            "provider_models",
            "agents",
        }:
            return True
        return self._busy

    def _complete_while_typing(self) -> bool:
        return False

    def _slash_active(self) -> bool:
        return self._panel is None and not self._busy and bool(self._slash_menu.matches)

    def _slash_menu_text(self) -> FormattedText:
        matches = self._slash_menu.matches
        if not matches:
            return FormattedText()
        selected = self._slash_menu.selected
        start = max(0, min(selected - 3, len(matches) - 7))
        visible = matches[start : start + 7]
        name_width = max(len(command.name) for command in visible) + 2
        fragments: list[tuple[str, str]] = []
        for offset, command in enumerate(visible):
            index = start + offset
            style = "class:selected" if index == selected else ""
            meta_style = style if index == selected else "class:secondary"
            fragments.extend(
                [
                    (style, f"  /{command.name:<{name_width}}"),
                    (meta_style, command.description),
                    ("", "\n" if offset + 1 < len(visible) else ""),
                ]
            )
        return FormattedText(fragments)

    def _move_slash(self, offset: int) -> None:
        self._slash_menu.move(offset)
        self._invalidate()

    def _dismiss_slash(self) -> None:
        self._slash_menu.dismiss(self.buffer.text)
        self._invalidate()

    def _accept_slash(self, *, execute: bool) -> None:
        command = self._slash_menu.current
        if command is None:
            return
        text = f"/{command.name}" + ("" if execute else " ")
        self.buffer.set_document(Document(text, len(text)), bypass_readonly=True)
        self._slash_menu.dismiss(text)
        if execute:
            self._spawn(self._submit_buffer())
        self._invalidate()

    def _provider_password_field(self) -> bool:
        return (
            self._panel == "provider_form"
            and self._provider_fields[self._provider_field][0] == "api_key"
        )

    def _dynamic_text(self) -> AnyFormattedText:
        if self._panel is not None:
            return self._panel_text()
        parts = [part for part in (self._activity, self._reasoning_summary()) if part]
        fragments = list(to_formatted_text("\n".join(parts)))
        if self._stream_text:
            if fragments:
                fragments.append(("", "\n"))
            fragments.extend(
                to_formatted_text(
                    streaming_assistant_message(self._stream_text, self.console.width)
                )
            )
        return FormattedText(fragments)

    def _todo_display(self) -> str:
        return todo_text(self._todos, expanded=self._todos_expanded)

    def _status_text(self) -> str:
        status = self._status
        if status is None:
            return "Starting my-code…"
        context = self._context_status
        context_usage = format_context_usage(context) if context is not None else "…"
        rendered = status_line(status, context_usage)
        return rendered + (
            f" · ! {self._status_warning}" if self._status_warning else ""
        )

    def _reasoning_summary(self) -> str:
        if not self._reasoning_parts:
            return ""
        return "Thinking · " + " ".join("".join(self._reasoning_parts).split())[-300:]

    def _panel_text(self) -> AnyFormattedText:
        if self._panel == "permission" and self._permission_request is not None:
            return permission_panel(
                self._permission_request, self._permission_mode, self._panel_index
            )
        if self._panel == "resume":
            return resume_panel(self._sessions, self._panel_index)
        if self._panel == "provider_select":
            return provider_select_panel(self._providers, self._panel_index)
        if self._panel == "provider_actions" and self._providers:
            return provider_actions_panel(
                self._providers[self._provider_selected_index], self._panel_index
            )
        if self._panel == "provider_remove_credential" and self._providers:
            return provider_remove_credential_panel(
                self._providers[self._provider_selected_index], self._panel_index
            )
        if self._panel == "provider_form" and self._provider_form is not None:
            return provider_form_panel(
                self._provider_form,
                self._provider_fields,
                self._provider_field,
                self.buffer.text,
                self._provider_models,
            )
        if self._panel == "provider_review" and self._provider_form is not None:
            return provider_review_panel(self._provider_form, self._panel_index)
        if self._panel == "provider_models":
            return provider_models_panel(self._provider_models, self._panel_index)
        if self._panel == "agents":
            return self._agent_panel_text()
        return ""

    def _agent_panel_text(self) -> str:
        if self._panel_index == 0:
            lines = ["Main session · F6 cycles through agent views"]
            lines.extend(
                ("○ " if item.status in {"succeeded", "failed", "cancelled"} else "● ")
                + f"{item.description} · {item.status} · "
                f"{'background' if item.background else 'foreground'}"
                for item in self._agents
            )
            lines.append("↑↓ select · Enter view · Esc close")
            return "\n".join(lines)
        index = self._panel_index - 1
        if index >= len(self._agents):
            self._panel_index = 0
            return "Main session"
        return render_agent_view(self._agents[index], scroll=self._agent_scroll)

    def _permission_selecting(self) -> bool:
        return self._panel == "permission" and self._permission_mode == "select"

    async def _submit_buffer(self) -> None:
        line = self.buffer.text
        if not line.strip():
            return
        self.buffer.reset()
        self._history.append_string(line)
        outcome = self.commands.dispatch(line, status=self.runtime.status())
        if outcome is not None:
            try:
                await self._handle_command(outcome)
            except Exception as error:
                await self._write(
                    system_message(f"Command failed: {error}", error=True)
                )
        else:
            self._foreground_task = asyncio.current_task()
            try:
                await self._run_turn(line, self.runtime.stream(line), user=True)
            finally:
                self._foreground_task = None

    async def _handle_command(self, outcome: CommandOutcome) -> None:
        if outcome.clear_screen:
            await self._write(welcome(self.runtime.status(), self.theme), clear=True)
        if outcome.message:
            await self._write(system_message(outcome.message))
        if outcome.show_context:
            context = self.runtime.context_status()
            self._context_status = context
            await self._write(system_message(render_context_status(context)))
        if outcome.compact_context:
            await self._run_compaction()
        if outcome.show_usage:
            usage = self.runtime.session_usage()
            self._context_status = usage.context
            await self._write(system_message(render_usage(usage)))
        if outcome.show_tools:
            await self._write(render_tools(self.runtime.capabilities()))
        if outcome.skill_operation is not None:
            if outcome.skill_operation == "reload":
                self._busy = True
                self._activity = "Reloading skills…"
                try:
                    capabilities = await self.runtime.reload_skills()
                finally:
                    self._busy = False
                    self._activity = ""
                    self._refresh_status()
            else:
                capabilities = self.runtime.capabilities()
            await self._write(render_skills(capabilities))
        if outcome.mcp_operation is not None:
            operation, server = outcome.mcp_operation
            if operation == "list":
                capabilities = self.runtime.capabilities()
            else:
                self._busy = True
                self._activity = f"MCP {operation} · {server}…"
                try:
                    capabilities = (
                        await self.runtime.refresh_mcp(server)
                        if operation == "refresh"
                        else await self.runtime.reconnect_mcp(server)
                    )
                finally:
                    self._busy = False
                    self._activity = ""
                    self._refresh_status()
            await self._write(render_mcp(capabilities))
        if outcome.show_tasks:
            await self._write(render_tasks(self.runtime.background_tasks()))
        if outcome.show_agents:
            self._open_agents()
        if outcome.open_session_picker:
            await self._open_resume()
        if outcome.open_provider_manager:
            self._open_provider()
        if outcome.should_exit:
            self.application.exit()

    async def _watch_background_notifications(self) -> None:
        stream = getattr(self.runtime, "stream_background_notifications", None)
        if stream is None:
            return
        invocation: list[TurnEvent] = []
        try:
            async for event in stream():
                if isinstance(event, BackgroundInvocationStarted):
                    invocation = []
                    self._saved_draft = self.buffer.text
                    self._busy = True
                    self._activity = "Handling background task…"
                    self._background_tools = {}
                elif isinstance(event, BackgroundInvocationFinished):
                    if event.error:
                        await self._write(
                            system_message(
                                f"Background continuation failed: {event.error}",
                                error=True,
                            )
                        )
                    self._busy = False
                    self._activity = ""
                    self._background_tools = {}
                    self._refresh_status()
                else:
                    invocation.append(event)
                    await self._consume_background_event(event)
                self._invalidate()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._write(
                system_message(f"Background watcher failed: {error}", error=True)
            )
            self._busy = False

    async def _watch_subagent_activity(self) -> None:
        stream = getattr(self.runtime, "stream_subagent_activity", None)
        if stream is None:
            return
        try:
            async for tasks in stream():
                self._agents = tasks
                if self._panel == "agents" and self._panel_index > len(tasks):
                    self._panel_index = 0
                self._invalidate()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._status_warning = f"agent watcher: {type(error).__name__}"
            self._invalidate()

    async def _run_compaction(self) -> None:
        self._busy = True
        self._activity = "Compacting conversation…"
        try:
            status = await self.runtime.compact()
            self._context_status = status
            await self._write(
                system_message(
                    "Conversation compacted.\n" + render_context_status(status)
                )
            )
        except Exception as error:
            await self._write(system_message(f"Compaction failed: {error}", error=True))
        finally:
            self._busy = False
            self._activity = ""
            self._refresh_status()

    async def _open_resume(self) -> None:
        self._sessions = await self.runtime.list_sessions()
        self._panel_index = 0
        self._open_panel("resume")

    def _open_provider(self) -> None:
        self._providers = self.runtime.providers()
        self._panel_index = next(
            (i for i, item in enumerate(self._providers) if item.active), 0
        )
        self._provider_selected_index = self._panel_index
        self._open_panel("provider_select")

    def _open_agents(self) -> None:
        self._agents = self.runtime.subagent_tasks()
        self._panel_index = 0
        self._agent_scroll = 0
        self._open_panel("agents")

    def _cycle_agent_view(self) -> None:
        self._agents = self.runtime.subagent_tasks()
        if self._panel != "agents":
            self._saved_draft = self.buffer.text
            self._panel = "agents"
            self._panel_index = 1 if self._agents else 0
        else:
            self._panel_index = (self._panel_index + 1) % (len(self._agents) + 1)
            if self._panel_index == 0:
                self._close_panel()
                return
        self._agent_scroll = 0
        self._invalidate()

    def _scroll_agent(self, offset: int | None) -> None:
        if self._panel != "agents" or self._panel_index == 0:
            return
        if offset is None:
            self._agent_scroll = 0
        else:
            self._agent_scroll = max(0, self._agent_scroll + offset)
        self._invalidate()

    def _open_panel(self, name: str) -> None:
        self._saved_draft = self.buffer.text
        self.buffer.set_document(Document(""), bypass_readonly=True)
        self._panel = name
        self._invalidate()

    def _close_panel(self) -> None:
        self._panel = None
        self._provider_form = None
        self.buffer.set_document(
            Document(self._saved_draft, len(self._saved_draft)), bypass_readonly=True
        )
        self._invalidate()

    def _move_panel(self, offset: int) -> None:
        if self._panel == "resume":
            size = len(self._sessions)
        elif self._panel == "permission" and self._permission_mode == "select":
            size = len(self._permission_options())
        elif self._panel == "provider_select":
            size = min(len(self._providers), 8) + 1
        elif self._panel == "provider_actions":
            provider = self._providers[self._provider_selected_index]
            size = 4 if provider.has_stored_key else 3
        elif self._panel == "provider_remove_credential":
            size = 2
        elif self._panel == "provider_review":
            size = 4
        elif self._panel == "provider_models":
            size = min(len(self._provider_models), 8)
        elif self._panel == "agents":
            size = len(self._agents) + 1
        else:
            size = 0
        if size:
            self._panel_index = (self._panel_index + offset) % size
            self._invalidate()

    async def _panel_enter(self) -> None:
        if self._panel == "permission":
            value = self.buffer.text.strip()
            if self._permission_mode == "select":
                self._choose_permission(self._permission_options()[self._panel_index])
            elif self._permission_mode == "feedback" and value:
                self._resolve_permission(PermissionConfirmation(False, value))
            elif self._permission_mode == "prefix" and value:
                await self._submit_permission_prefix(value)
        elif self._panel == "resume" and self._sessions:
            session_id = self._sessions[self._panel_index].session_id
            try:
                resumed = await self.runtime.resume_session(session_id)
                self._status = resumed.status
                self._context_status = self.runtime.context_status()
                self._todos = resumed.status.todos
                self._panel = None
                self.buffer.set_document(Document(""), bypass_readonly=True)
                await self._write(welcome(resumed.status, self.theme), clear=True)
                await self._render_history(resumed.history)
            except Exception as error:
                await self._write(
                    system_message(
                        f"Failed to resume conversation: {error}", error=True
                    )
                )
                self._close_panel()
        elif self._panel == "provider_select":
            if self._panel_index == min(len(self._providers), 8):
                self._provider_selected_index = -1
                self._start_provider_form(ProviderForm())
            else:
                self._provider_selected_index = self._panel_index
                self._panel = "provider_actions"
                self._panel_index = 0
                self.buffer.set_document(Document(""), bypass_readonly=True)
                self._invalidate()
        elif self._panel == "provider_actions":
            provider = self._providers[self._provider_selected_index]
            if self._panel_index == 0:
                self._provider_form = ProviderForm.from_view(provider)
                await self._save_provider()
            elif self._panel_index == 1:
                self._start_provider_form(ProviderForm.from_view(provider))
            elif provider.has_stored_key and self._panel_index == 2:
                self._panel = "provider_remove_credential"
                self._panel_index = 1
                self._invalidate()
            else:
                self._provider_back()
        elif self._panel == "provider_remove_credential":
            if self._panel_index == 0:
                await self._remove_provider_credential()
            else:
                self._provider_back()
        elif self._panel == "provider_form":
            await self._advance_provider(1)
        elif self._panel == "provider_review":
            if self._panel_index == 0:
                await self._save_provider()
            elif self._panel_index == 1:
                await self._refresh_provider()
            elif self._panel_index == 2:
                self._start_provider_form(
                    self._provider_form, fields=PROVIDER_ADVANCED_FIELDS
                )
            else:
                self._panel = "provider_select"
                self._panel_index = max(self._provider_selected_index, 0)
                self._invalidate()
        elif self._panel == "provider_models" and self._provider_models:
            assert self._provider_form is not None
            self._provider_form.model = self._provider_models[self._panel_index]
            self._panel = "provider_review"
            self._panel_index = 0
            self.buffer.set_document(Document(""), bypass_readonly=True)
            self._invalidate()
        elif self._panel == "agents":
            if self._panel_index == 0:
                self._close_panel()
            else:
                self._agent_scroll = 0
                self._invalidate()

    def _start_provider_form(
        self,
        form: ProviderForm | None,
        *,
        fields: tuple[tuple[str, str], ...] = PROVIDER_CORE_FIELDS,
    ) -> None:
        if form is None:
            return
        self._provider_form = form
        self._provider_fields = fields
        self._provider_field = 0
        self._panel = "provider_form"
        self._load_provider_field()

    def _load_provider_field(self) -> None:
        assert self._provider_form is not None
        name = self._provider_fields[self._provider_field][0]
        value = cast(str, getattr(self._provider_form, name))
        self.buffer.set_document(Document(value, len(value)), bypass_readonly=True)
        self._invalidate()

    async def _advance_provider(self, offset: int) -> None:
        form = self._provider_form
        if form is None:
            return
        name = self._provider_fields[self._provider_field][0]
        setattr(form, name, self.buffer.text)
        target = self._provider_field + offset
        if target >= len(self._provider_fields):
            self._panel = "provider_review"
            self._panel_index = 0
            self.buffer.set_document(Document(""), bypass_readonly=True)
            self._invalidate()
            return
        self._provider_field = max(0, target)
        self._load_provider_field()

    async def _refresh_provider(self) -> None:
        form = self._provider_form
        if form is None:
            return
        if self._provider_selected_index < 0:
            await self._write(
                system_message("Save this provider before discovering models.")
            )
            return
        try:
            view = await self.runtime.refresh_provider_models(form.provider_id.strip())
            self._provider_models = view.models
        except Exception as error:
            await self._write(
                system_message(f"Model discovery failed: {error}", error=True)
            )
            return
        if not self._provider_models:
            await self._write(system_message("No models were discovered."))
            return
        self._panel = "provider_models"
        self._panel_index = next(
            (
                i
                for i, model in enumerate(self._provider_models[:8])
                if model == form.model
            ),
            0,
        )
        self.buffer.set_document(Document(""), bypass_readonly=True)
        self._invalidate()

    async def _save_provider(self) -> None:
        form = self._provider_form
        if form is None:
            return
        try:
            status = await self.runtime.configure_provider(form.build_update())
        except Exception as error:
            await self._write(
                system_message(f"Provider configuration failed: {error}", error=True)
            )
            return
        self._status = status
        self._context_status = self.runtime.context_status()
        self._panel = None
        self._provider_form = None
        self.buffer.set_document(
            Document(self._saved_draft, len(self._saved_draft)), bypass_readonly=True
        )
        await self._write(
            system_message(f"Using provider {status.provider_id!r} · {status.model}")
        )
        self._invalidate()

    async def _remove_provider_credential(self) -> None:
        provider = self._providers[self._provider_selected_index]
        try:
            status = await self.runtime.remove_provider_credential(provider.id)
            providers = self.runtime.providers()
        except Exception as error:
            await self._write(
                system_message(f"Failed to remove saved API key: {error}", error=True)
            )
            return
        refreshed = next(item for item in providers if item.id == provider.id)
        self._status = status
        self._context_status = self.runtime.context_status()
        self._providers = providers
        self._panel = None
        self.buffer.set_document(
            Document(self._saved_draft, len(self._saved_draft)), bypass_readonly=True
        )
        if refreshed.credential_source.value == "environment":
            message = (
                f"Saved API key for {provider.id!r} removed. "
                "An environment API key remains active."
            )
        else:
            message = (
                f"Saved API key for {provider.id!r} removed. "
                "The provider is now not configured."
            )
        await self._write(system_message(message))
        self._invalidate()

    def _provider_back(self) -> None:
        if self._panel == "provider_actions":
            self._panel = "provider_select"
            self._panel_index = max(self._provider_selected_index, 0)
        elif self._panel == "provider_remove_credential":
            self._panel = "provider_actions"
            self._panel_index = 2
        elif self._panel == "provider_models":
            self._panel = "provider_review"
            self._panel_index = 1
        elif self._panel in {"provider_form", "provider_review"}:
            if self._provider_selected_index >= 0:
                self._panel = "provider_actions"
                self._panel_index = 1
            else:
                self._panel = "provider_select"
                self._panel_index = min(len(self._providers), 8)
        self.buffer.set_document(Document(""), bypass_readonly=True)
        self._invalidate()

    async def _ask_permission(
        self, request: PermissionRequest
    ) -> PermissionConfirmation:
        if self._permission_future is not None:
            raise RuntimeError("A permission request is already active")
        self._saved_draft = self.buffer.text
        self.buffer.set_document(Document(""), bypass_readonly=True)
        self._permission_request = request
        self._permission_mode = "select"
        self._panel_index = 1
        self._panel = "permission"
        self._permission_future = asyncio.get_running_loop().create_future()
        self._invalidate()
        try:
            return await self._permission_future
        finally:
            self._permission_future = None
            self._permission_request = None
            self._panel = None
            self.buffer.set_document(
                Document(self._saved_draft, len(self._saved_draft)),
                bypass_readonly=True,
            )
            self._invalidate()

    def _choose_permission(self, choice: str) -> None:
        request = self._permission_request
        if choice == "allow":
            self._resolve_permission(PermissionConfirmation(True))
        elif choice == "deny":
            self._resolve_permission(PermissionConfirmation(False))
        elif choice == "feedback":
            self._permission_mode = "feedback"
            self.buffer.set_document(Document(""), bypass_readonly=True)
        elif choice == "remember" and request is not None:
            if request.tool_name == "Bash":
                self._permission_mode = "prefix"
                self.buffer.set_document(Document(""), bypass_readonly=True)
            elif request.suggestions:
                self._resolve_permission(
                    PermissionConfirmation(True, updates=request.suggestions)
                )
        self._invalidate()

    def _permission_options(self) -> tuple[str, ...]:
        request = self._permission_request
        options = ["allow", "deny", "feedback"]
        if request is not None and (request.tool_name == "Bash" or request.suggestions):
            options.append("remember")
        return tuple(options)

    async def _submit_permission_prefix(self, raw: str) -> None:
        try:
            content = validate_bash_rule_content(raw)
        except ValueError as error:
            await self._write(
                system_message(f"Invalid Bash prefix: {error}", error=True)
            )
            self.buffer.reset()
            return
        update = PermissionUpdate.add_rules(
            (
                permission_rule_for_destination(
                    "Bash",
                    PermissionBehavior.ALLOW,
                    PermissionUpdateDestination.LOCAL,
                    content,
                ),
            ),
            destination=PermissionUpdateDestination.LOCAL,
        )
        self._resolve_permission(PermissionConfirmation(True, updates=(update,)))

    def _resolve_permission(self, result: PermissionConfirmation) -> None:
        future = self._permission_future
        if future is not None and not future.done():
            future.set_result(result)

    def _on_text_changed(self, _: Buffer) -> None:
        if self._panel is not None or self._busy:
            return
        self._slash_menu.update(self.buffer.text, self.commands)
        self._invalidate()
        mention = mention_at_cursor(self.buffer.text, self.buffer.cursor_position)
        self._suggestion_revision += 1
        revision = self._suggestion_revision
        if mention is None:
            self._mention_span = None
            self._path_suggestions = ()
            return
        start, end, query = mention
        self._mention_span = (start, end)
        self._spawn(self._load_paths(revision, query, start, end))

    async def _load_paths(
        self, revision: int, query: str, start: int, end: int
    ) -> None:
        suggestions = await self.runtime.suggest_paths(query)
        if revision != self._suggestion_revision or self._mention_span != (start, end):
            return
        self._path_suggestions = suggestions
        if suggestions:
            self.buffer.start_completion(select_first=True)

    async def _render_history(self, history: tuple[HistoryEntry, ...]) -> None:
        for entry in history:
            if isinstance(entry, HistoryText):
                renderable = (
                    user_message(entry.text, self.theme)
                    if entry.role == "user"
                    else assistant_message(entry.text)
                    if entry.role == "assistant"
                    else system_message(entry.text)
                )
            elif isinstance(entry, HistoryReasoning):
                renderable = reasoning_message(entry.presentation)
            else:
                assert isinstance(entry, HistoryToolCall)
                renderable = tool_message(
                    entry.use, entry.result, is_error=entry.is_error
                )
            await self._write(renderable)

    def _refresh_status(self) -> None:
        warnings: list[str] = []
        try:
            self._status = self.runtime.status()
            self._todos = self._status.todos
        except Exception as error:
            warnings.append(f"status: {type(error).__name__}")
        try:
            self._context_status = self.runtime.context_status()
        except Exception as error:
            warnings.append(f"context: {type(error).__name__}")
        self._status_warning = ", ".join(warnings)
        self._invalidate()


class MyCodeTui:
    def __init__(self, runtime: ChatService) -> None:
        self.app = MyCodeApp(runtime)

    async def run(self) -> None:
        await self.app.run_async()


__all__ = ["MyCodeApp", "MyCodeTui"]
