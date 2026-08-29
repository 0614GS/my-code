"""Native, non-full-screen prompt_toolkit host with Rich scrollback output."""

from __future__ import annotations

import asyncio
from time import monotonic
from typing import Any, cast

from prompt_toolkit.application import Application, in_terminal, run_in_terminal
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
from rich.padding import Padding

from my_code.chat.events import (
    BackgroundInvocationFinished,
    BackgroundInvocationStarted,
    TurnEvent,
)
from my_code.chat.history import (
    HistoryEntry,
    HistoryText,
    HistoryToolCall,
)
from my_code.chat.permissions import PermissionRequest
from my_code.chat.service import ChatService
from my_code.chat.status import ContextStatus, RuntimeStatus
from my_code.chat.views import SubagentTaskView
from my_code.config.validation import validate_base_url
from my_code.features.file_mentions.models import PathSuggestion
from my_code.model.primitives import validate_provider_id
from my_code.permissions.models import (
    PermissionConfirmation,
)
from my_code.providers.manager import ModelView, ProviderView
from my_code.sessions.catalog import SessionSummary
from my_code.tui.activity import ToolActivityGroup
from my_code.tui.commands import CommandOutcome, SlashCommandRegistry
from my_code.tui.completion import mention_at_cursor
from my_code.tui.composer import ComposerCompleter, ContinuationIndent, SlashMenuState
from my_code.tui.key_bindings import build_key_bindings
from my_code.tui.layout import build_terminal_layout
from my_code.tui.panels import (
    agent_select_panel,
    full_access_panel,
    model_picker_panel,
    permission_panel,
    provider_actions_panel,
    provider_checking_panel,
    provider_form_panel,
    provider_models_panel,
    provider_probe_failure_panel,
    provider_protocol_panel,
    provider_remove_credential_panel,
    provider_review_panel,
    provider_select_panel,
    render_picker,
    resume_panel,
)
from my_code.tui.picker import PickerState, PickerView
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
    ProviderWizard,
)
from my_code.tui.terminal import terminal_supports_true_color
from my_code.tui.theme import TuiTheme
from my_code.tui.transcript import TranscriptPager
from my_code.tui.turns import TurnFlowMixin
from my_code.tui.widgets import (
    assistant_message,
    history_message,
    status_line,
    streaming_assistant_message,
    streaming_renderable,
    system_message,
    todo_snapshot,
    tool_activity_message,
    welcome,
)

#: Minimum interval between dynamic-region redraws while streaming. Per-token
#: deltas would otherwise trigger a full Markdown re-render of the accumulated
#: text on every token, starving the single-threaded UI loop.
_STREAM_INVALIDATE_INTERVAL = 0.04


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
        self._stream_invalidate_pending = False
        self._last_stream_invalidate = 0.0
        self._activity = ""
        self._stream_text = ""
        self._reasoning_parts: list[str] = []
        self._todos = ()
        self._tool_activity: ToolActivityGroup | None = None
        self._status: RuntimeStatus | None = None
        self._context_status: ContextStatus | None = None
        self._status_warning = ""
        self._agents: tuple[SubagentTaskView, ...] = ()
        self._agent_scroll = 0
        self._agent_task_id: str | None = None
        self._panel: str | None = None
        self._panel_picker = PickerState()
        self._sessions: tuple[SessionSummary, ...] = ()
        self._providers: tuple[ProviderView, ...] = ()
        self._provider_form: ProviderForm | None = None
        self._provider_wizard: ProviderWizard | None = None
        self._provider_probe_task: asyncio.Task[object] | None = None
        self._provider_selected_index = 0
        self._provider_fields = PROVIDER_CORE_FIELDS
        self._provider_field = 0
        self._provider_models: tuple[str, ...] = ()
        self._models: tuple[ModelView, ...] = ()
        self._permission_request: PermissionRequest | None = None
        self._permission_future: asyncio.Future[PermissionConfirmation] | None = None
        self._permission_mode = "select"
        self._full_access_resolved: asyncio.Event | None = None
        self._saved_draft = ""
        self._path_suggestions: tuple[PathSuggestion, ...] = ()
        self._mention_span: tuple[int, int] | None = None
        self._suggestion_revision = 0
        self._foreground_task: asyncio.Task[None] | None = None
        self._startup_ready = asyncio.Event()
        self._history_ready = asyncio.Event()
        self._startup_error: Exception | None = None
        self._submission_pending = False
        self._has_scrollback_output = False
        self._last_scrollback_was_user = False
        self._transcript_pager: TranscriptPager | None = None
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
        prompt_text = "› "
        prompt = BeforeInput(FormattedText([("class:prompt", prompt_text)]))
        continuation = ContinuationIndent(len(prompt_text))
        password = ConditionalProcessor(
            PasswordProcessor(), Condition(lambda: self._provider_password_field())
        )
        self.input_control = BufferControl(
            buffer=self.buffer, input_processors=[prompt, continuation, password]
        )
        self.key_bindings = build_key_bindings(self)
        terminal_layout = build_terminal_layout(
            input_control=self.input_control,
            key_bindings=self.key_bindings,
            dynamic_text=self._dynamic_text,
            status_text=self._status_display,
            interaction_text=self._interaction_text,
            has_interaction=self._interaction_active,
            input=input,
            output=output,
            theme=self.theme,
        )
        self.application: Application[None] = terminal_layout.application
        self.body = terminal_layout.body
        self.completions_menu = terminal_layout.completions_menu
        self.interaction_menu = terminal_layout.interaction_menu
        self.slash_menu = terminal_layout.slash_menu
        self.runtime.set_permission_handler(self._ask_permission)

    @property
    def _panel_index(self) -> int:
        """Compatibility surface for tests and focused panel transitions."""

        return self._panel_picker.index

    @_panel_index.setter
    def _panel_index(self, value: int) -> None:
        self._panel_picker.reset(value)

    async def run_async(self) -> None:
        view = self.runtime.current_session_view()
        self._status = view.status
        self._context_status = self.runtime.context_status()
        self._todos = view.status.todos
        await self._write(welcome(view.status, self.theme))
        self._activity = "Initializing capabilities…"
        current_mode = getattr(self.runtime, "current_permission_mode", None)
        if current_mode is not None and current_mode().requires_confirmation:
            self._open_full_access_confirmation()
        self._running = True

        def start_background() -> None:
            self._spawn(self._restore_startup_history(view.history))
            self._spawn(self._initialize_capabilities())

        try:
            await self.application.run_async(pre_run=start_background)
        finally:
            self._running = False
            if self._transcript_pager is not None:
                self._transcript_pager.close()
            for task in tuple(self._tasks):
                task.cancel()
            if self._tasks:
                await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    async def _initialize_capabilities(self) -> None:
        try:
            view = await self.runtime.initialize()
            self._status = view.status
            self._context_status = self.runtime.context_status()
            self._todos = view.status.todos
            await self._history_ready.wait()
            self._activity = ""
            await self._write(
                system_message(
                    f"Ready · {view.status.tool_count} tools · "
                    f"{view.status.skill_count} skills"
                )
            )
            self._spawn(self._watch_background_notifications())
            self._spawn(self._watch_subagent_activity())
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._startup_error = error
            self._activity = ""
            await self._write(
                system_message(f"Capability initialization failed: {error}", error=True)
            )
            self.application.exit(exception=error)
        finally:
            self._startup_ready.set()
            self._invalidate()

    async def _restore_startup_history(self, history: tuple[HistoryEntry, ...]) -> None:
        try:
            await self._render_history(history)
            for entry in history:
                if isinstance(entry, HistoryText) and entry.role == "user":
                    self._history.append_string(entry.text)
        finally:
            self._history_ready.set()

    def _spawn(self, coroutine: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine)
        self._tasks.add(cast(asyncio.Task[object], task))
        task.add_done_callback(self._tasks.discard)
        return task

    async def _write(self, renderable: RenderableType, *, clear: bool = False) -> None:
        def render() -> None:
            if clear:
                self.console.clear()
                self._has_scrollback_output = False
                self._last_scrollback_was_user = False
            is_user = isinstance(renderable, Padding)
            if (
                self._has_scrollback_output
                and not self._last_scrollback_was_user
                and not is_user
            ):
                self.console.print()
            self.console.print(renderable)
            self._has_scrollback_output = True
            self._last_scrollback_was_user = is_user

        if self._running:
            await run_in_terminal(render)
        else:
            render()

    def _invalidate(self) -> None:
        self.application.invalidate()

    def _invalidate_streaming(self) -> None:
        """Coalesce high-frequency streaming redraws to a bounded frame rate.

        Streaming text deltas would otherwise trigger one full Markdown
        re-render of the accumulated text per token. Instead, the first delta
        after the interval elapses redraws immediately; the rest are merged
        into a single deferred redraw scheduled for the next interval window.
        """
        if self._stream_invalidate_pending:
            return
        elapsed = monotonic() - self._last_stream_invalidate
        delay = _STREAM_INVALIDATE_INTERVAL - elapsed
        if delay <= 0:
            self._last_stream_invalidate = monotonic()
            self._invalidate()
            return
        self._stream_invalidate_pending = True
        asyncio.get_running_loop().call_later(delay, self._flush_stream_invalidate)

    def _flush_stream_invalidate(self) -> None:
        self._stream_invalidate_pending = False
        if not self._running:
            return
        self._last_stream_invalidate = monotonic()
        self._invalidate()

    async def _open_transcript(self) -> None:
        if self._transcript_pager is not None:
            return
        pager = TranscriptPager(
            self.runtime,
            input=self.application.input,
            output=self.application.output,
        )
        self._transcript_pager = pager
        try:
            async with in_terminal():
                await pager.run_async()
        finally:
            pager.close()
            self._transcript_pager = None
            self._invalidate()

    def _composer_read_only(self) -> bool:
        if self._panel == "permission":
            return self._permission_mode == "select"
        if self._panel in {
            "full_access",
            "resume",
            "provider_select",
            "provider_actions",
            "provider_remove_credential",
            "provider_review",
            "provider_protocol",
            "provider_probe_failure",
            "provider_checking",
            "agents",
        }:
            return True
        return self._busy

    def _complete_while_typing(self) -> bool:
        return False

    def _slash_active(self) -> bool:
        return self._panel is None and not self._busy and bool(self._slash_menu.matches)

    def _interaction_active(self) -> bool:
        if self._panel == "agents" and self._agent_task_id is not None:
            return False
        return self._panel is not None or self._slash_active()

    def _interaction_text(self) -> AnyFormattedText:
        if self._panel is not None:
            return self._panel_text()
        return self._slash_menu_text()

    def _slash_menu_text(self) -> FormattedText:
        matches = self._slash_menu.matches
        if not matches:
            return FormattedText()
        start, visible = self._slash_menu.visible(7)
        selected = self._slash_menu.selected
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
        if self._panel == "agents" and self._agent_task_id is not None:
            return self._agent_panel_text()
        parts = [
            part
            for part in (
                "" if self._tool_activity else self._activity,
                self._reasoning_summary(),
            )
            if part
        ]
        fragments = list(to_formatted_text("\n".join(parts)))
        if self._tool_activity:
            if fragments:
                fragments.append(("", "\n"))
            fragments.extend(
                to_formatted_text(
                    streaming_renderable(
                        tool_activity_message(
                            self._tool_activity, tail=6, expand_diffs=False
                        ),
                        self.console.width,
                    )
                )
            )
        if self._stream_text:
            if fragments:
                fragments.append(("", "\n"))
            fragments.extend(
                to_formatted_text(
                    streaming_assistant_message(self._stream_text, self.console.width)
                )
            )
        return FormattedText(fragments)

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

    def _status_display(self) -> FormattedText:
        status = self._status
        if status is None:
            return FormattedText([("class:secondary", "Starting my-code…")])
        context = self._context_status
        usage = format_context_usage(context) if context is not None else "…"
        left = (
            f"{status.model} · {status.context_entry_count} context entries    {usage}"
        )
        if self._status_warning:
            left += f" · ! {self._status_warning}"
        labels = {
            "default": ("Ask for me", "class:secondary"),
            "acceptEdits": ("Approve edits", "class:success"),
            "bypassPermissions": ("Full access", "class:error"),
        }
        label, style = labels.get(
            status.permission_mode, (status.permission_mode, "class:secondary")
        )
        right = f"{label} · Shift+Tab"
        padding = max(2, self.console.width - len(left) - len(right))
        if len(left) + len(right) + padding > self.console.width:
            keep = max(0, self.console.width - len(right) - padding - 1)
            left = left[:keep].rstrip() + ("…" if keep else "")
        return FormattedText(
            [("class:secondary", left), ("", " " * padding), (style, right)]
        )

    def _reasoning_summary(self) -> str:
        if not self._reasoning_parts:
            return ""
        return "Thinking · " + " ".join("".join(self._reasoning_parts).split())[-300:]

    def _panel_view(self) -> PickerView | None:
        if self._panel == "full_access":
            return full_access_panel()
        if self._panel == "permission" and self._permission_request is not None:
            content = permission_panel(self._permission_request, self._permission_mode)
            return content if isinstance(content, PickerView) else None
        if self._panel == "resume":
            content = resume_panel(self._sessions)
            return content if isinstance(content, PickerView) else None
        if self._panel == "provider_select":
            return provider_select_panel(self._providers)
        if self._panel == "provider_protocol":
            return provider_protocol_panel()
        if self._panel == "provider_actions" and self._providers:
            return provider_actions_panel(
                self._providers[self._provider_selected_index]
            )
        if self._panel == "provider_remove_credential" and self._providers:
            return provider_remove_credential_panel(
                self._providers[self._provider_selected_index]
            )
        if self._panel == "provider_review" and self._provider_form is not None:
            return provider_review_panel(
                self._provider_form,
                connection_verified=(
                    self._provider_wizard.connection_verified
                    if self._provider_wizard is not None
                    else False
                ),
                model_count=(
                    len(self._provider_wizard.probe_result.models)
                    if self._provider_wizard is not None
                    and self._provider_wizard.probe_result is not None
                    else len(tuple(dict.fromkeys(self._provider_form.model.split())))
                ),
            )
        if self._panel == "provider_probe_failure":
            result = (
                self._provider_wizard.probe_result
                if self._provider_wizard is not None
                else None
            )
            return provider_probe_failure_panel(
                result.error_message
                if result and result.error_message
                else "Unknown error"
            )
        if self._panel == "provider_models":
            return provider_models_panel(self._provider_models, self.buffer.text)
        if self._panel == "model_select":
            return model_picker_panel(self._models, self.buffer.text)
        if self._panel == "agents" and self._agent_task_id is None:
            return agent_select_panel(self._agents)
        return None

    def _panel_text(self) -> AnyFormattedText:
        view = self._panel_view()
        if view is not None:
            return render_picker(view, self._panel_picker, self.console.width)
        if self._panel == "permission" and self._permission_request is not None:
            return cast(
                AnyFormattedText,
                permission_panel(self._permission_request, self._permission_mode),
            )
        if self._panel == "resume":
            return cast(AnyFormattedText, resume_panel(self._sessions))
        if self._panel == "provider_form" and self._provider_form is not None:
            return provider_form_panel(
                self._provider_form,
                self._provider_fields,
                self._provider_field,
                self.buffer.text,
                self._provider_models,
            )
        if self._panel == "provider_checking":
            return provider_checking_panel()
        if self._panel == "agents":
            return self._agent_panel_text()
        return ""

    def _agent_panel_text(self) -> str:
        if self._agent_task_id is None:
            return ""
        task = next(
            (item for item in self._agents if item.task_id == self._agent_task_id),
            None,
        )
        if task is None:
            return "Selected Subagent is no longer available · Esc main"
        return render_agent_view(
            task,
            scroll=self._agent_scroll,
            width=self.console.width,
            theme=self.theme,
        )

    def _permission_selecting(self) -> bool:
        return self._panel == "permission" and self._permission_mode == "select"

    def _cycle_permission_mode(self) -> None:
        switch = getattr(self.runtime, "cycle_permission_mode", None)
        if switch is None:
            return
        result = switch()
        if result.requires_confirmation:
            self._open_full_access_confirmation()
        else:
            self._refresh_status()

    def _open_full_access_confirmation(self) -> None:
        if self._panel is not None:
            return
        self._saved_draft = self.buffer.text
        self.buffer.set_document(Document(""), bypass_readonly=True)
        self._panel = "full_access"
        self._panel_index = 0
        self._full_access_resolved = asyncio.Event()
        self._invalidate()

    def _resolve_full_access(self, allow: bool) -> None:
        if self._panel != "full_access":
            return
        confirm = getattr(self.runtime, "confirm_full_access", None)
        if confirm is not None:
            confirm(allow)
        resolved = self._full_access_resolved
        self._full_access_resolved = None
        self._panel = None
        self.buffer.set_document(
            Document(self._saved_draft, len(self._saved_draft)), bypass_readonly=True
        )
        self._refresh_status()
        if resolved is not None:
            resolved.set()

    async def _submit_buffer(self) -> None:
        if self._submission_pending:
            return
        line = self.buffer.text
        if not line.strip():
            return
        self._submission_pending = True
        try:
            if not self._startup_ready.is_set() or not self._history_ready.is_set():
                self._busy = True
                self._activity = "Initializing capabilities…"
                self._invalidate()
                await asyncio.gather(
                    self._startup_ready.wait(), self._history_ready.wait()
                )
                self._busy = False
                self._activity = ""
                if self._startup_error is not None:
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
        finally:
            self._submission_pending = False
            if self._startup_ready.is_set():
                self._busy = False

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
        if outcome.open_model_picker:
            self._open_model_picker()
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
                    self._stream_text = ""
                    self._reasoning_parts = []
                    self._tool_activity = None
                elif isinstance(event, BackgroundInvocationFinished):
                    partial_text = self._retire_transient_content()
                    self._busy = False
                    self._activity = ""
                    await self._interrupt_and_flush_tools()
                    if event.error:
                        await self._write(
                            system_message(
                                f"Background continuation failed: {event.error}",
                                error=True,
                            )
                        )
                    if partial_text:
                        await self._write(assistant_message(partial_text))
                    self._refresh_status()
                else:
                    invocation.append(event)
                    await self._consume_background_event(event)
                self._invalidate_for_event(event)
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

    def _open_model_picker(self) -> None:
        self._models = self.runtime.models()
        self._panel_index = next(
            (index for index, item in enumerate(self._models) if item.current), 0
        )
        self._open_panel("model_select")

    def _open_agents(self) -> None:
        self._agents = self.runtime.subagent_tasks()
        self._panel_index = 0
        self._agent_scroll = 0
        self._agent_task_id = None
        self._open_panel("agents")

    def _cycle_agent_view(self) -> None:
        if self._panel == "permission":
            return
        self._agents = self.runtime.subagent_tasks()
        if self._panel != "agents":
            self._saved_draft = self.buffer.text
            self._panel = "agents"
            self._agent_task_id = self._agents[0].task_id if self._agents else None
            self._panel_index = 0
        else:
            ids = [item.task_id for item in self._agents]
            if self._agent_task_id is None:
                self._agent_task_id = ids[0] if ids else None
            elif self._agent_task_id in ids and ids.index(
                self._agent_task_id
            ) + 1 < len(ids):
                self._agent_task_id = ids[ids.index(self._agent_task_id) + 1]
            else:
                self._close_panel()
                return
        self._agent_scroll = 0
        self._invalidate()

    def _scroll_agent(self, offset: int | None) -> None:
        if self._panel != "agents" or self._agent_task_id is None:
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
        if self._panel == "agents":
            self._agent_task_id = None
        self._panel = None
        if self._provider_wizard is not None:
            self._provider_wizard.clear_sensitive()
        self._provider_wizard = None
        self._provider_form = None
        self.buffer.set_document(
            Document(self._saved_draft, len(self._saved_draft)), bypass_readonly=True
        )
        self._invalidate()

    def _move_panel(self, offset: int) -> None:
        view = self._panel_view()
        if view is not None:
            self._panel_picker.move(view.rows, offset)
            self._invalidate()

    async def _panel_enter(self) -> None:
        view = self._panel_view()
        row = self._panel_picker.current(view.rows) if view is not None else None
        action = row.key if row is not None else None
        if self._panel == "full_access":
            self._resolve_full_access(action == "allow")
        elif self._panel == "permission":
            value = self.buffer.text.strip()
            if self._permission_mode == "select" and action is not None:
                self._choose_permission(action)
            elif self._permission_mode == "feedback" and value:
                self._resolve_permission(PermissionConfirmation(False, value))
        elif self._panel == "resume" and action is not None:
            try:
                resumed = await self.runtime.resume_session(action)
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
        elif self._panel == "model_select" and action is not None:
            try:
                status = await self.runtime.select_model(action)
            except Exception as error:
                await self._write(
                    system_message(f"Model selection failed: {error}", error=True)
                )
                return
            self._status = status
            self._context_status = self.runtime.context_status()
            self._status_warning = self._context_status.warning or ""
            self._close_panel()
            await self._write(system_message(f"Using model {status.model!r}"))
        elif self._panel == "provider_select" and action is not None:
            if action == "add":
                self._provider_selected_index = -1
                self._provider_wizard = ProviderWizard.new()
                self._provider_form = self._provider_wizard.form
                self._panel = "provider_protocol"
                self._panel_index = 0
                self._invalidate()
            else:
                provider_id = action.removeprefix("provider:")
                self._provider_selected_index = next(
                    i
                    for i, provider in enumerate(self._providers)
                    if provider.id == provider_id
                )
                self._panel = "provider_actions"
                self._panel_index = 0
                self.buffer.set_document(Document(""), bypass_readonly=True)
                self._invalidate()
        elif self._panel == "provider_actions" and action is not None:
            provider = self._providers[self._provider_selected_index]
            if action == "use":
                await self._select_provider(provider.id)
            elif action == "configure":
                self._provider_wizard = ProviderWizard.edit(provider)
                self._provider_form = self._provider_wizard.form
                self._panel = "provider_protocol"
                self._panel_index = (
                    0 if provider.protocol.value == "anthropic-messages" else 1
                )
                self._invalidate()
            elif action == "remove":
                self._panel = "provider_remove_credential"
                self._panel_index = 1
                self._invalidate()
            else:
                self._provider_back()
        elif self._panel == "provider_remove_credential" and action is not None:
            if action == "remove":
                await self._remove_provider_credential()
            else:
                self._provider_back()
        elif self._panel == "provider_protocol" and action is not None:
            assert self._provider_form is not None
            self._provider_form.protocol = action
            self._start_provider_connection_form()
        elif self._panel == "provider_form":
            await self._advance_provider(1)
        elif self._panel == "provider_review" and action is not None:
            if action == "save":
                await self._save_provider()
            elif action == "models":
                if (
                    self._provider_wizard is not None
                    and self._provider_wizard.probe_result is not None
                ):
                    self._show_probe_models()
                else:
                    self._start_provider_form(
                        self._provider_form, fields=(("model", "Model"),)
                    )
            elif action == "advanced":
                self._start_provider_form(
                    self._provider_form, fields=PROVIDER_ADVANCED_FIELDS
                )
            else:
                self._cancel_provider_wizard()
        elif self._panel == "provider_probe_failure" and action is not None:
            if action == "retry":
                await self._probe_provider()
            elif action in {"base_url", "api_key"}:
                self._start_provider_connection_form()
                self._provider_field = next(
                    i
                    for i, (name, _label) in enumerate(self._provider_fields)
                    if name == action
                )
                self._load_provider_field()
            elif action == "manual":
                self._start_provider_form(
                    self._provider_form, fields=(("model", "Model"),)
                )
            else:
                self._cancel_provider_wizard()
        elif self._panel == "provider_models" and action is not None:
            assert self._provider_form is not None
            self._provider_form.model = action
            if self._provider_wizard is not None:
                self._provider_wizard.model_filter = ""
            self._panel = "provider_review"
            self._panel_index = 0
            self.buffer.set_document(Document(""), bypass_readonly=True)
            self._invalidate()
        elif self._panel == "agents" and action is not None:
            if action == "main":
                self._close_panel()
            else:
                self._agent_task_id = action
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

    def _start_provider_connection_form(self) -> None:
        wizard = self._provider_wizard
        if wizard is None:
            return
        fields = PROVIDER_CORE_FIELDS[1:] if wizard.editing else PROVIDER_CORE_FIELDS
        self._start_provider_form(wizard.form, fields=fields)

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
        try:
            if name == "provider_id":
                validate_provider_id(form.provider_id.strip())
            elif name == "base_url" and form.base_url.strip():
                validate_base_url(form.base_url.strip())
            elif name == "api_key" and any(
                character.isspace() for character in form.api_key.strip()
            ):
                raise ValueError("API key must not contain whitespace")
        except ValueError as error:
            await self._write(system_message(f"Invalid provider: {error}", error=True))
            return
        target = self._provider_field + offset
        if target >= len(self._provider_fields):
            if self._provider_fields in {
                PROVIDER_CORE_FIELDS,
                PROVIDER_CORE_FIELDS[1:],
            }:
                await self._probe_provider()
            elif self._provider_fields == (("model", "Model"),):
                if self._provider_wizard is not None:
                    self._provider_wizard.use_manual_model(form.model)
                self._open_provider_review()
            else:
                self._open_provider_review()
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
            (i for i, model in enumerate(self._provider_models) if model == form.model),
            0,
        )
        self.buffer.set_document(Document(""), bypass_readonly=True)
        self._invalidate()

    async def _probe_provider(self) -> None:
        wizard = self._provider_wizard
        if wizard is None:
            return
        try:
            request = wizard.probe_request()
        except Exception as error:
            await self._write(system_message(f"Invalid provider: {error}", error=True))
            return
        self._panel = "provider_checking"
        self.buffer.set_document(Document(""), bypass_readonly=True)
        self._provider_probe_task = cast(asyncio.Task[object], asyncio.current_task())
        self._invalidate()
        try:
            result = await self.runtime.probe_provider(request)
        except asyncio.CancelledError:
            self._start_provider_connection_form()
            self._provider_field = len(self._provider_fields) - 1
            self._load_provider_field()
            return
        finally:
            self._provider_probe_task = None
        wizard.accept_probe(result)
        if wizard.connection_verified:
            self._open_provider_review()
        else:
            self._panel = "provider_probe_failure"
            self._panel_index = 0
            self._invalidate()

    def _show_probe_models(self) -> None:
        wizard = self._provider_wizard
        if wizard is None or wizard.probe_result is None:
            return
        wizard.model_filter = ""
        self._provider_models = wizard.filtered_models()
        self._panel = "provider_models"
        self._panel_index = next(
            (
                i
                for i, model in enumerate(self._provider_models)
                if model == wizard.form.model
            ),
            0,
        )
        self.buffer.set_document(Document(""), bypass_readonly=True)
        self._invalidate()

    def _open_provider_review(self) -> None:
        self._panel = "provider_review"
        self._panel_index = 0
        self.buffer.set_document(Document(""), bypass_readonly=True)
        self._invalidate()

    async def _save_provider(self) -> None:
        form = self._provider_form
        if form is None:
            return
        try:
            update = (
                self._provider_wizard.build_update()
                if self._provider_wizard is not None
                else form.build_update()
            )
            probe_result = (
                self._provider_wizard.probe_result
                if self._provider_wizard is not None
                and self._provider_wizard.connection_verified
                else None
            )
            status = await self.runtime.configure_provider(update, probe_result)
        except Exception as error:
            await self._write(
                system_message(f"Provider configuration failed: {error}", error=True)
            )
            return
        self._status = status
        self._context_status = self.runtime.context_status()
        self._status_warning = self._context_status.warning or ""
        self._panel = None
        if self._provider_wizard is not None:
            self._provider_wizard.clear_sensitive()
        self._provider_wizard = None
        self._provider_form = None
        self.buffer.set_document(
            Document(self._saved_draft, len(self._saved_draft)), bypass_readonly=True
        )
        await self._write(
            system_message(f"Using provider {status.provider_id!r} · {status.model}")
        )
        self._invalidate()

    async def _select_provider(self, provider_id: str) -> None:
        try:
            status = await self.runtime.select_provider(provider_id)
        except Exception as error:
            await self._write(
                system_message(f"Provider selection failed: {error}", error=True)
            )
            return
        self._status = status
        self._context_status = self.runtime.context_status()
        self._status_warning = self._context_status.warning or ""
        self._close_panel()
        await self._write(
            system_message(f"Using provider {status.provider_id!r} · {status.model}")
        )

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
        self._status = status
        self._context_status = self.runtime.context_status()
        self._providers = providers
        self._panel = None
        self.buffer.set_document(
            Document(self._saved_draft, len(self._saved_draft)), bypass_readonly=True
        )
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
        elif self._panel == "provider_checking":
            if self._provider_probe_task is not None:
                self._provider_probe_task.cancel()
            return
        elif self._panel == "provider_probe_failure":
            self._start_provider_connection_form()
            self._provider_field = len(self._provider_fields) - 1
            self._load_provider_field()
            return
        elif self._panel == "provider_form":
            if self._provider_fields in {
                PROVIDER_CORE_FIELDS,
                PROVIDER_CORE_FIELDS[1:],
            }:
                self._panel = "provider_protocol"
                self._panel_index = (
                    0
                    if self._provider_form is not None
                    and self._provider_form.protocol == "anthropic-messages"
                    else 1
                )
            else:
                self._open_provider_review()
                return
        elif self._panel == "provider_review":
            if (
                self._provider_wizard is not None
                and self._provider_wizard.probe_result is not None
            ):
                self._show_probe_models()
            else:
                self._start_provider_form(
                    self._provider_form, fields=(("model", "Model"),)
                )
            return
        elif self._panel == "provider_protocol":
            self._cancel_provider_wizard()
            return
        self.buffer.set_document(Document(""), bypass_readonly=True)
        self._invalidate()

    def _cancel_provider_wizard(self) -> None:
        if self._provider_wizard is not None:
            self._provider_wizard.clear_sensitive()
        self._provider_wizard = None
        self._provider_form = None
        if self._provider_selected_index >= 0:
            self._panel = "provider_actions"
            self._panel_index = 1
        else:
            self._panel = "provider_select"
            self._panel_index = len(self._providers)
        self.buffer.set_document(Document(""), bypass_readonly=True)
        self._invalidate()

    async def _ask_permission(
        self, request: PermissionRequest
    ) -> PermissionConfirmation:
        if self._full_access_resolved is not None:
            await self._full_access_resolved.wait()
        if self._permission_future is not None:
            raise RuntimeError("A permission request is already active")
        restore_panel = self._panel
        restore_picker = PickerState(
            self._panel_picker.index,
            self._panel_picker.offset,
            self._panel_picker.selected_key,
        )
        draft = self._saved_draft if restore_panel == "agents" else self.buffer.text
        self._saved_draft = draft
        self.buffer.set_document(Document(""), bypass_readonly=True)
        self._permission_request = request
        self._permission_mode = "select"
        self._panel_index = 2 if request.tool_name == "Bash" else 1
        self._panel = "permission"
        self._permission_future = asyncio.get_running_loop().create_future()
        self._invalidate()
        try:
            return await self._permission_future
        finally:
            self._permission_future = None
            self._permission_request = None
            self._panel = restore_panel
            self._panel_picker = restore_picker
            self.buffer.set_document(
                Document(self._saved_draft, len(self._saved_draft)),
                bypass_readonly=True,
            )
            self._invalidate()

    def _choose_permission(self, choice: str) -> None:
        request = self._permission_request
        if choice == "allow":
            self._resolve_permission(PermissionConfirmation(True))
        elif choice == "second":
            if request is not None and request.tool_name == "Bash":
                self._resolve_permission(
                    PermissionConfirmation(True, updates=request.suggestions)
                )
            else:
                self._resolve_permission(PermissionConfirmation(False))
        elif choice == "third":
            if request is not None and request.tool_name == "Bash":
                self._resolve_permission(PermissionConfirmation(False))
            else:
                self._permission_mode = "feedback"
                self.buffer.set_document(Document(""), bypass_readonly=True)
        elif choice == "deny":
            self._resolve_permission(PermissionConfirmation(False))
        elif choice == "feedback":
            self._permission_mode = "feedback"
            self.buffer.set_document(Document(""), bypass_readonly=True)
        elif choice == "remember" and request is not None:
            if request.tool_name != "Bash" and request.suggestions:
                self._resolve_permission(
                    PermissionConfirmation(True, updates=request.suggestions)
                )
        self._invalidate()

    def _permission_options(self) -> tuple[str, ...]:
        request = self._permission_request
        if request is not None and request.tool_name == "Bash":
            return ("allow", "second", "third")
        options = ["allow", "second", "third"]
        if request is not None and request.suggestions:
            options.append("remember")
        return tuple(options)

    def _resolve_permission(self, result: PermissionConfirmation) -> None:
        future = self._permission_future
        if future is not None and not future.done():
            future.set_result(result)

    def _on_text_changed(self, _: Buffer) -> None:
        if self._panel == "model_select":
            self._panel_picker.reset()
            self._invalidate()
            return
        if self._panel == "provider_models":
            wizard = self._provider_wizard
            if wizard is not None:
                wizard.model_filter = self.buffer.text
                self._provider_models = wizard.filtered_models()
                self._panel_index = 0
                self._invalidate()
            return
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
        previous_todos = ()
        group: ToolActivityGroup | None = None
        pending_todos: tuple[Any, ...] | None = None
        for index, entry in enumerate(history):
            if isinstance(entry, HistoryToolCall):
                if entry.todos is not None and not entry.is_error:
                    pending_todos = entry.todos
                else:
                    if group is None:
                        group = ToolActivityGroup()
                    group.start(entry.tool_use_id, entry.use)
                    group.finish(
                        entry.tool_use_id, entry.result, is_error=entry.is_error
                    )
                if entry.ends_tool_batch and pending_todos is not None:
                    if group:
                        await self._write(tool_activity_message(group))
                        group = None
                    if pending_todos != previous_todos:
                        await self._write(todo_snapshot(pending_todos))
                        previous_todos = pending_todos
                    pending_todos = None
            else:
                if group:
                    await self._write(tool_activity_message(group))
                    group = None
                if pending_todos is not None:
                    if pending_todos != previous_todos:
                        await self._write(todo_snapshot(pending_todos))
                        previous_todos = pending_todos
                    pending_todos = None
                await self._write(history_message(entry, self.theme))
            if index and index % 20 == 0:
                await asyncio.sleep(0)
        if group:
            await self._write(tool_activity_message(group))
        if pending_todos is not None and pending_todos != previous_todos:
            await self._write(todo_snapshot(pending_todos))

    def _refresh_status(self) -> None:
        warnings: list[str] = []
        try:
            self._status = self.runtime.status()
            self._todos = self._status.todos
        except Exception as error:
            warnings.append(f"status: {type(error).__name__}")
        try:
            self._context_status = self.runtime.context_status()
            if self._context_status.warning:
                warnings.append(self._context_status.warning)
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
