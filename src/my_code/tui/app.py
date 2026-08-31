"""Native, non-full-screen prompt_toolkit host with Rich scrollback output."""

from __future__ import annotations

import asyncio
from time import monotonic
from typing import Any, cast

from prompt_toolkit.application import Application, in_terminal
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import (
    AnyFormattedText,
    FormattedText,
    StyleAndTextTuples,
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
from prompt_toolkit.utils import get_cwidth
from rich.console import Console, RenderableType
from rich.padding import Padding

from my_code.application.contracts.events import (
    BackgroundInvocationFinished,
    BackgroundInvocationStarted,
    CompactionCompleted,
    CompactionStarted,
    TurnEvent,
)
from my_code.application.contracts.history import (
    HistoryContextGroup,
    HistoryEntry,
    HistoryText,
    HistoryToolCall,
)
from my_code.application.contracts.inputs import PathSuggestion
from my_code.application.contracts.permissions import PermissionRequest
from my_code.application.contracts.questions import QuestionAnswer, QuestionRequest
from my_code.application.contracts.status import ApplicationStatus, ContextUsageView
from my_code.application.contracts.views import SubagentTaskView, TranscriptView
from my_code.application.service import ApplicationService
from my_code.model.display import DisplayDensity
from my_code.permissions.models import (
    PermissionConfirmation,
    PermissionPromptCategory,
)
from my_code.providers.manager import ModelView, ProviderView
from my_code.sessions.catalog import SessionSummary
from my_code.tools.base import ToolExecutionError
from my_code.tui.activity import ToolActivityGroup
from my_code.tui.activity_flow import ActivityFlowMixin
from my_code.tui.activity_indicator import ActivityOwner
from my_code.tui.block_flow import TurnBlockCoordinator
from my_code.tui.commands import (
    CommandConcurrency,
    CommandOutcome,
    SlashCommandRegistry,
)
from my_code.tui.completion import mention_at_cursor
from my_code.tui.composer import ComposerCompleter, ContinuationIndent, SlashMenuState
from my_code.tui.key_bindings import build_key_bindings
from my_code.tui.layout import build_terminal_layout
from my_code.tui.panel_flow import PanelFlowMixin
from my_code.tui.panels import (
    agent_select_panel,
    full_access_panel,
    model_picker_panel,
    permission_mode_panel,
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
    view_mode_panel,
)
from my_code.tui.picker import PickerRow, PickerState, PickerView
from my_code.tui.presentation import (
    compaction_activity_label,
    compaction_completed_message,
    format_context_usage,
    render_agent_view,
    render_context_card,
    render_mcp,
    render_skills,
    render_status_card,
    render_tasks,
    render_tools,
    render_usage_card,
)
from my_code.tui.provider_screen import (
    PROVIDER_CORE_FIELDS,
    ProviderForm,
    ProviderWizard,
)
from my_code.tui.rendering import (
    RenderCoordinator,
    ScrollbackWriter,
    StreamingMarkdownProjector,
)
from my_code.tui.terminal import terminal_supports_true_color
from my_code.tui.theme import TuiTheme
from my_code.tui.transcript import TranscriptPager
from my_code.tui.turns import TurnFlowMixin
from my_code.tui.widgets import (
    assistant_message,
    block_separator,
    command_echo,
    detailed_tool_call_message,
    history_message,
    status_line,
    streaming_renderable,
    system_message,
    todo_snapshot,
    tool_activity_message,
    welcome,
    work_separator,
)

#: Minimum interval between dynamic-region redraws while streaming. Per-token
#: deltas would otherwise trigger a full Markdown re-render of the accumulated
#: text on every token, starving the single-threaded UI loop.
_STREAM_INVALIDATE_INTERVAL = 0.04


class _ChildTranscriptSource:
    def __init__(self, view: Any, task_id: str) -> None:
        self._view = view
        self._task_id = task_id

    def current_transcript_view(self) -> TranscriptView:
        return cast(TranscriptView, self._view(self._task_id))


class MyCodeApp(ActivityFlowMixin, PanelFlowMixin, TurnFlowMixin):
    """Inline terminal application; canonical state stays in ApplicationService."""

    def __init__(
        self,
        application: ApplicationService,
        *,
        commands: SlashCommandRegistry | None = None,
        input: Input | None = None,
        output: Output | None = None,
        console: Console | None = None,
    ) -> None:
        self.application = application
        self.commands = commands or SlashCommandRegistry.default()
        self.theme = TuiTheme.detect()
        self.console = console or Console(
            color_system="truecolor" if terminal_supports_true_color() else "auto"
        )
        self._busy = False
        self._agent_active = False
        self._running = False
        self._stream_invalidate_pending = False
        self._last_stream_invalidate = 0.0
        self._initialize_activity_flow()
        self._startup_activity_owner: ActivityOwner | None = None
        self._stream_text = ""
        self._stream_plan = ""
        self._stream_frame = FormattedText()
        self._stream_frame_revision = 0
        self._stream_projector = StreamingMarkdownProjector()
        self._render_coordinator = RenderCoordinator(
            self._stream_projector, frame_interval=_STREAM_INVALIDATE_INTERVAL
        )
        self._scrollback_writer = ScrollbackWriter(self.console)
        self._reasoning_parts: list[str] = []
        self._todos = ()
        self._tool_activity: ToolActivityGroup | None = None
        self._blocks = TurnBlockCoordinator()
        self._status: ApplicationStatus | None = None
        self._context_status: ContextUsageView | None = None
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
        self._question_request: QuestionRequest | None = None
        self._question_answers: list[QuestionAnswer] = []
        self._question_index = 0
        self._question_other = False
        self._question_future: asyncio.Future[tuple[QuestionAnswer, ...]] | None = None
        self._pending_plan: str | None = None
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
        view_mode = getattr(self.application, "view_mode", None)
        self._display_density = (
            view_mode() if view_mode is not None else DisplayDensity.CONCISE
        )
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
            activity_text=self._activity_text,
            queue_text=self._queue_text,
            status_text=self._status_display,
            interaction_text=self._interaction_text,
            has_interaction=self._interaction_active,
            input=input,
            output=output,
            theme=self.theme,
        )
        self.terminal_application: Application[None] = terminal_layout.application
        self.body = terminal_layout.body
        self.completions_menu = terminal_layout.completions_menu
        self.interaction_menu = terminal_layout.interaction_menu
        self.slash_menu = terminal_layout.slash_menu
        self.application.set_permission_handler(self._ask_permission)
        set_question_handler = getattr(self.application, "set_question_handler", None)
        if set_question_handler is not None:
            set_question_handler(self._ask_question)

    @property
    def _panel_index(self) -> int:
        """Compatibility surface for tests and focused panel transitions."""

        return self._panel_picker.index

    @_panel_index.setter
    def _panel_index(self, value: int) -> None:
        self._panel_picker.reset(value)

    async def run_async(self) -> None:
        view = self.application.current_session_view()
        self._status = view.status
        self._todos = view.status.todos
        await self._write(welcome(view.status, self.theme))
        self._startup_activity_owner = self._begin_activity(
            "Initializing capabilities…"
        )
        current_mode = getattr(self.application, "current_permission_mode", None)
        if current_mode is not None and current_mode().requires_confirmation:
            self._open_full_access_confirmation()
        self._scrollback_writer.seed(
            self._has_scrollback_output, self._last_scrollback_was_user
        )
        self._running = True

        def start_background() -> None:
            self._spawn(self._tick_activity())
            self._spawn(self._restore_startup_history(view.history))
            self._spawn(self._initialize_capabilities())

        try:
            await self.terminal_application.run_async(pre_run=start_background)
        finally:
            self._running = False
            if self._transcript_pager is not None:
                self._transcript_pager.close()
            for task in tuple(self._tasks):
                task.cancel()
            if self._tasks:
                await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
            await self._render_coordinator.close()
            await self._scrollback_writer.close()

    async def _initialize_capabilities(self) -> None:
        try:
            view = await self.application.initialize()
            self._status = view.status
            self._todos = view.status.todos
            await self._history_ready.wait()
            self._end_activity(self._startup_activity_owner)
            self._startup_activity_owner = None
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
            self._end_activity(self._startup_activity_owner)
            self._startup_activity_owner = None
            await self._write(
                system_message(f"Capability initialization failed: {error}", error=True)
            )
            self.terminal_application.exit(exception=error)
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
            await self._scrollback_writer.write(renderable, clear=clear)
            self._has_scrollback_output = True
            self._last_scrollback_was_user = isinstance(renderable, Padding)
        else:
            render()

    async def _write_many(self, renderables: tuple[RenderableType, ...]) -> None:
        if not renderables:
            return
        if self._running:
            await self._scrollback_writer.write_many(renderables)
            self._has_scrollback_output = True
            self._last_scrollback_was_user = isinstance(renderables[-1], Padding)
            return
        for renderable in renderables:
            await self._write(renderable)

    def _invalidate(self) -> None:
        self.terminal_application.invalidate()

    def _invalidate_streaming(self) -> None:
        """Coalesce high-frequency streaming redraws to a bounded frame rate.

        Streaming text deltas would otherwise trigger one full Markdown
        re-render of the accumulated text per token. Instead, the first delta
        after the interval elapses redraws immediately; the rest are merged
        into a single deferred redraw scheduled for the next interval window.
        """
        self._prepare_stream_frame()
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

    def _prepare_stream_frame(self, *, structural: bool = False) -> None:
        if not self._running:
            return
        if not self._stream_text:
            self._stream_frame = FormattedText()
            if structural:
                self._render_coordinator.clear()
            return
        self._render_coordinator.request(
            self._stream_text,
            self.console.width,
            self._accept_stream_frame,
            structural=structural,
        )

    def _accept_stream_frame(self, revision: int, frame: FormattedText) -> None:
        if revision < self._stream_frame_revision:
            return
        self._stream_frame_revision = revision
        self._stream_frame = frame
        self._invalidate()

    async def _open_transcript(self) -> None:
        if self._transcript_pager is not None:
            return
        source: object = self.application
        if self._panel == "agents" and self._agent_task_id is not None:
            child_view = getattr(self.application, "subagent_transcript_view", None)
            if child_view is not None:
                source = _ChildTranscriptSource(child_view, self._agent_task_id)
        pager = TranscriptPager(
            cast(Any, source),
            input=self.terminal_application.input,
            output=self.terminal_application.output,
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
            "view_select",
            "permission_mode_select",
            "agents",
        }:
            return True
        return self._busy and not self._agent_active

    def _complete_while_typing(self) -> bool:
        return False

    def _slash_active(self) -> bool:
        return (
            self._panel is None
            and (not self._busy or self._agent_active)
            and bool(self._slash_menu.matches)
        )

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
        fragments: StyleAndTextTuples = []
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

    def _recall_pending_input(self) -> bool:
        recall = getattr(self.application, "recall_latest_input", None)
        if recall is None:
            return False
        prompt = recall()
        if prompt is None:
            return False
        self.buffer.set_document(Document(prompt, len(prompt)), bypass_readonly=True)
        self._invalidate()
        return True

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
        fragments: StyleAndTextTuples = []
        reasoning = self._reasoning_summary()
        if reasoning:
            if fragments:
                fragments.append(("", "\n"))
            fragments.extend(to_formatted_text(reasoning))
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
            frame = (
                self._stream_frame
                if self._running
                else self._stream_projector.project(
                    self._stream_text, self.console.width
                )
            )
            fragments.extend(to_formatted_text(frame))
        return FormattedText(fragments)

    def _activity_text(self) -> FormattedText:
        return self._activity_indicator.text()

    def _queue_text(self) -> FormattedText:
        queued = getattr(self.application, "queued_inputs", lambda: ())()
        if not queued:
            return FormattedText()
        fragments: list[tuple[str, str]] = []
        for index, item in enumerate(queued[:3]):
            state = str(item.state)
            marker = {"preparing": "…", "queued": "↳", "failed": "!"}.get(state, "·")
            summary = " ".join(item.prompt.split())
            if len(summary) > 72:
                summary = summary[:71] + "…"
            suffix = f" · {item.error}" if item.error else ""
            fragments.append(("class:secondary", f"{marker} {summary}{suffix}"))
            if index + 1 < min(3, len(queued)):
                fragments.append(("", "\n"))
        if len(queued) > 3:
            fragments.extend(
                [("", "\n"), ("class:secondary", f"+{len(queued) - 3} queued")]
            )
        return FormattedText(fragments)

    def _status_text(self) -> str:
        status = self._status
        if status is None:
            return "Starting my-code…"
        context = self._context_status
        context_usage = format_context_usage(context) if context is not None else None
        rendered = status_line(status, context_usage)
        return rendered + (
            f" · ! {self._status_warning}" if self._status_warning else ""
        )

    def _status_display(self) -> FormattedText:
        status = self._status
        if status is None:
            return FormattedText([("class:secondary", "Starting my-code…")])
        context = self._context_status
        left = f"{status.model} · {status.context_entry_count} context entries"
        if context is not None:
            left += f"    {format_context_usage(context)}"
        if self._status_warning:
            left += f" · ! {self._status_warning}"
        fragments: list[tuple[str, str]] = [("class:secondary", left)]
        if status.collaboration_mode == "plan":
            indicator = "Plan"
            width = max(20, self.console.width)
            gap = max(1, width - get_cwidth(left) - get_cwidth(indicator))
            fragments.append(("class:secondary", " " * gap))
            fragments.append(("class:plan", indicator))
        return FormattedText(fragments)

    def _reasoning_summary(self) -> str:
        if not self._reasoning_parts:
            return ""
        content = "\n\n".join(self._reasoning_parts)
        lines = content.splitlines()[-10:]
        if not lines:
            return "Thinking…"
        return "Thinking · " + "\n".join(lines)

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
        if self._panel == "view_select":
            return view_mode_panel(self._display_density)
        if self._panel == "permission_mode_select":
            return permission_mode_panel(self.application.permission_modes())
        if (
            self._panel == "question"
            and self._question_request is not None
            and not self._question_other
        ):
            question = self._question_request.questions[self._question_index]
            return PickerView(
                f"{question.header} · {question.question}",
                tuple(
                    PickerRow(option.label, f"{option.label} — {option.description}")
                    for option in question.options
                )
                + (PickerRow("__other__", "Other — enter a custom answer"),),
                f"Question {self._question_index + 1}/"
                f"{len(self._question_request.questions)} · "
                "Enter select · Esc cancel",
            )
        if self._panel == "plan_action":
            return PickerView(
                "Implement proposed plan",
                (
                    PickerRow("current", "Implement in current context"),
                    PickerRow("fresh", "Implement in a fresh context"),
                    PickerRow("stay", "Stay in Plan mode"),
                ),
                "Enter select · Esc stay in Plan",
            )
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
        if (
            self._panel == "question"
            and self._question_other
            and self._question_request is not None
        ):
            question = self._question_request.questions[self._question_index]
            return FormattedText(
                [
                    ("class:heading", f"{question.header} · Other"),
                    ("", "\nEnter a custom answer, then press Enter · Esc cancel"),
                ]
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
        confirm = getattr(self.application, "confirm_full_access", None)
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
                self._invalidate()
                await asyncio.gather(
                    self._startup_ready.wait(), self._history_ready.wait()
                )
                self._busy = False
                if self._startup_error is not None:
                    return
            outcome = self.commands.dispatch(line, status=self.application.status())
            concurrency = self.commands.concurrency(line)
            if (
                outcome is not None
                and self._agent_active
                and concurrency is CommandConcurrency.EXCLUSIVE
            ):
                await self._write(
                    system_message("Wait for the active Agent turn to finish.")
                )
                return
            self.buffer.reset()
            if outcome is not None:
                self._history.append_string(line)
                try:
                    await self._handle_command(outcome, command_line=line)
                except Exception as error:
                    await self._write(
                        system_message(f"Command failed: {error}", error=True)
                    )
            else:
                queue = getattr(self.application, "queue_input", None)
                interactive = getattr(self.application, "stream_interactive", None)
                if queue is None or interactive is None:
                    self._history.append_string(line)
                    self._foreground_task = asyncio.current_task()
                    try:
                        await self._run_turn(
                            line, self.application.stream(line), user=True
                        )
                    finally:
                        self._foreground_task = None
                else:
                    queue(line)
                    if self._foreground_task is None or self._foreground_task.done():
                        self._foreground_task = self._spawn(
                            self._run_interactive_inputs()
                        )
                    self._invalidate()
        finally:
            self._submission_pending = False
            if self._startup_ready.is_set():
                if not self._agent_active:
                    self._busy = False

    async def _run_interactive_inputs(self) -> None:
        try:
            await self._run_turn("", self.application.stream_interactive(), user=False)
        finally:
            self._foreground_task = None
            queued = getattr(self.application, "queued_inputs", lambda: ())()
            if queued and any(str(item.state) != "failed" for item in queued):
                self._foreground_task = self._spawn(self._run_interactive_inputs())
            self._invalidate()

    async def _handle_command(
        self, outcome: CommandOutcome, *, command_line: str = ""
    ) -> None:
        echo_pending = bool(command_line)

        async def emit(renderable: RenderableType) -> None:
            nonlocal echo_pending
            renderables = (
                (command_echo(command_line), renderable)
                if echo_pending
                else (renderable,)
            )
            echo_pending = False
            await self._write_many(renderables)

        if outcome.clear_screen:
            await self._write(
                welcome(self.application.status(), self.theme), clear=True
            )
            echo_pending = False
        if outcome.message:
            await emit(system_message(outcome.message))
        if outcome.show_status:
            status = self.application.status()
            context = self._command_context_status()
            self._status = status
            self._context_status = context
            await emit(render_status_card(status, context))
        if outcome.show_context:
            context = self._command_context_status()
            self._context_status = context
            await emit(render_context_card(context))
        if outcome.compact_context:
            if echo_pending:
                await self._write(command_echo(command_line))
                echo_pending = False
            await self._run_compaction()
        if outcome.show_usage:
            usage = self.application.session_usage()
            self._context_status = usage.context
            await emit(render_usage_card(usage))
        if outcome.show_tools:
            await emit(render_tools(self.application.capabilities()))
        if outcome.view_operation is not None:
            message = await self._change_view_mode(outcome.view_operation)
            await emit(system_message(message))
        if outcome.skill_operation is not None:
            if outcome.skill_operation == "reload":
                self._busy = True
                activity_owner = self._begin_activity("Reloading skills…")
                try:
                    capabilities = await self.application.reload_skills()
                finally:
                    self._busy = False
                    self._end_activity(activity_owner)
                    self._refresh_status()
            else:
                capabilities = self.application.capabilities()
            await emit(render_skills(capabilities))
        if outcome.mcp_operation is not None:
            operation, server = outcome.mcp_operation
            if operation == "list":
                capabilities = self.application.capabilities()
            else:
                self._busy = True
                activity_owner = self._begin_activity(f"MCP {operation} · {server}…")
                try:
                    capabilities = (
                        await self.application.refresh_mcp(server)
                        if operation == "refresh"
                        else await self.application.reconnect_mcp(server)
                    )
                finally:
                    self._busy = False
                    self._end_activity(activity_owner)
                    self._refresh_status()
            await emit(render_mcp(capabilities))
        if outcome.show_tasks:
            await emit(render_tasks(self.application.background_tasks()))
        if echo_pending and (
            outcome.show_agents
            or outcome.open_session_picker
            or outcome.open_provider_manager
            or outcome.open_model_picker
            or outcome.open_view_picker
            or outcome.open_permission_picker
        ):
            await self._write(command_echo(command_line))
            echo_pending = False
        if outcome.show_agents:
            self._open_agents()
        if outcome.open_session_picker:
            await self._open_resume()
        if outcome.open_provider_manager:
            self._open_provider()
        if outcome.open_model_picker:
            self._open_model_picker()
        if outcome.open_view_picker:
            self._open_view_picker()
        if outcome.open_permission_picker:
            self._open_permission_picker()
        if outcome.should_exit:
            self.terminal_application.exit()

    async def _change_view_mode(self, operation: str) -> str:
        requested = DisplayDensity.from_view_mode(operation)
        if requested is self._display_density:
            return f"View mode · {requested.view_mode}"
        await self._flush_tool_activity()
        await self._flush_unclassified_blocks()
        setter = getattr(self.application, "set_view_mode", None)
        if setter is None:
            raise RuntimeError("Runtime does not support view preferences")
        setter(requested)
        previous = self._display_density
        self._display_density = requested
        view = self.application.current_session_view()
        self._status = view.status
        self._todos = view.status.todos
        self._blocks.reset_group()
        await self._write(welcome(view.status, self.theme), clear=True)
        await self._render_history(view.history)
        return (
            f"View mode changed · {previous.view_mode} → "
            f"{requested.view_mode} · session re-rendered"
        )

    def _command_context_status(self) -> ContextUsageView:
        """Prefer a fresh snapshot, but remain readable during an open tool pair."""

        try:
            return self.application.context_status()
        except Exception:
            if self._context_status is None:
                raise
            return self._context_status

    def _refresh_context_status_if_visible(self) -> ContextUsageView | None:
        """Recalculate only after a user action has exposed context usage."""

        if self._context_status is None:
            return None
        self._context_status = self.application.context_status()
        return self._context_status

    async def _watch_background_notifications(self) -> None:
        stream = getattr(self.application, "stream_background_notifications", None)
        if stream is None:
            return
        invocation: list[TurnEvent] = []
        try:
            async for event in stream():
                if isinstance(event, BackgroundInvocationStarted):
                    invocation = []
                    self._blocks.reset_group()
                    self._saved_draft = self.buffer.text
                    self._agent_active = True
                    self._busy = True
                    self._begin_agent_activity("Handling background task…")
                    self._stream_text = ""
                    self._reasoning_parts = []
                    self._tool_activity = None
                elif isinstance(event, BackgroundInvocationFinished):
                    partial_text = self._retire_transient_content()
                    self._agent_active = False
                    self._busy = False
                    self._end_agent_activity()
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
            self._agent_active = False
            self._busy = False
            self._end_agent_activity()

    async def _watch_subagent_activity(self) -> None:
        stream = getattr(self.application, "stream_subagent_activity", None)
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
        activity_owner: ActivityOwner | None = None
        try:
            async for event in self.application.stream_compaction():
                if isinstance(event, CompactionStarted):
                    activity_owner = self._begin_activity(
                        compaction_activity_label(event.trigger)
                    )
                elif isinstance(event, CompactionCompleted):
                    self._context_status = event.status
                    await self._write(
                        system_message(
                            compaction_completed_message(event.trigger, event.status)
                        )
                    )
        except Exception as error:
            await self._write(system_message(f"Compaction failed: {error}", error=True))
        finally:
            self._busy = False
            self._end_activity(activity_owner)
            self._refresh_status()

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

    async def _ask_question(
        self, request: QuestionRequest
    ) -> tuple[QuestionAnswer, ...]:
        if self._question_future is not None or self._panel is not None:
            raise ToolExecutionError("Another exclusive TUI panel is active.")
        self._question_request = request
        self._question_answers = []
        self._question_index = 0
        self._question_other = False
        self._panel = "question"
        self._panel_picker.reset()
        self._question_future = asyncio.get_running_loop().create_future()
        self._invalidate()
        try:
            return await self._question_future
        finally:
            self._question_future = None
            self._question_request = None
            self._question_answers = []
            self._question_other = False
            self._panel = None
            self.buffer.set_document(Document(""), bypass_readonly=True)
            self._invalidate()

    def _answer_question(self, answer: str) -> None:
        request = self._question_request
        future = self._question_future
        if request is None or future is None or future.done():
            return
        question = request.questions[self._question_index]
        self._question_answers.append(QuestionAnswer(question.id, answer))
        self._question_index += 1
        self._question_other = False
        self.buffer.set_document(Document(""), bypass_readonly=True)
        self._panel_picker.reset()
        if self._question_index == len(request.questions):
            future.set_result(tuple(self._question_answers))
        self._invalidate()

    def _cancel_question(self) -> None:
        future = self._question_future
        if future is not None and not future.done():
            future.set_exception(
                ToolExecutionError("Question was cancelled by the user.")
            )

    def _choose_permission(self, choice: str) -> None:
        request = self._permission_request
        if choice == "allow":
            self._resolve_permission(PermissionConfirmation(True))
        elif choice == "second":
            if (
                request is not None
                and request.tool_name == "Bash"
                and request.category is not PermissionPromptCategory.SANDBOX_ESCALATION
            ):
                self._resolve_permission(
                    PermissionConfirmation(True, updates=request.suggestions)
                )
            else:
                self._resolve_permission(PermissionConfirmation(False))
        elif choice == "third":
            if (
                request is not None
                and request.category is PermissionPromptCategory.SANDBOX_ESCALATION
            ):
                self._permission_mode = "feedback"
                self.buffer.set_document(Document(""), bypass_readonly=True)
            elif request is not None and request.tool_name == "Bash":
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
        if self._panel is not None or (self._busy and not self._agent_active):
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
        suggestions = await self.application.suggest_paths(query)
        if revision != self._suggestion_revision or self._mention_span != (start, end):
            return
        self._path_suggestions = suggestions
        if suggestions:
            self.buffer.start_completion(select_first=True)

    async def _render_history(self, history: tuple[HistoryEntry, ...]) -> None:
        previous_todos = ()
        group: ToolActivityGroup | None = None
        pending_todos: tuple[Any, ...] | None = None
        renderables: list[RenderableType] = []
        work_visible = False
        for entry in history:
            if isinstance(entry, HistoryContextGroup):
                if self._display_density.includes(DisplayDensity.DETAILED):
                    if group:
                        renderables.append(tool_activity_message(group))
                        group = None
                    renderables.append(history_message(entry, self.theme))
                    work_visible = True
                continue
            if isinstance(entry, HistoryToolCall):
                if (
                    self._display_density.includes(DisplayDensity.DETAILED)
                    and entry.input is not None
                ):
                    if group:
                        renderables.append(tool_activity_message(group))
                        work_visible = True
                        group = None
                    renderables.append(
                        detailed_tool_call_message(
                            entry.name or entry.use.display_name, entry.input
                        )
                    )
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
                        renderables.append(tool_activity_message(group))
                        work_visible = True
                        group = None
                    if pending_todos != previous_todos:
                        renderables.append(todo_snapshot(pending_todos))
                        work_visible = True
                        previous_todos = pending_todos
                    pending_todos = None
            else:
                if group:
                    renderables.append(tool_activity_message(group))
                    work_visible = True
                    group = None
                if pending_todos is not None:
                    if pending_todos != previous_todos:
                        renderables.append(todo_snapshot(pending_todos))
                        work_visible = True
                        previous_todos = pending_todos
                    pending_todos = None
                if isinstance(entry, HistoryText):
                    if entry.role == "user":
                        if self._display_density.includes(DisplayDensity.DETAILED):
                            renderables.append(block_separator("User input"))
                        work_visible = False
                    elif entry.is_final_answer and work_visible:
                        renderables.append(
                            block_separator("Assistant response")
                            if self._display_density.includes(DisplayDensity.DETAILED)
                            else work_separator()
                        )
                        work_visible = False
                    elif entry.is_final_answer and self._display_density.includes(
                        DisplayDensity.DETAILED
                    ):
                        renderables.append(block_separator("Assistant response"))
                    elif entry.role == "assistant" and not entry.is_final_answer:
                        work_visible = True
                else:
                    work_visible = True
                renderables.append(history_message(entry, self.theme))
        if group:
            renderables.append(tool_activity_message(group))
            work_visible = True
        if pending_todos is not None and pending_todos != previous_todos:
            renderables.append(todo_snapshot(pending_todos))
        await self._write_many(tuple(renderables))

    def _refresh_status(self) -> None:
        warnings: list[str] = []
        try:
            self._status = self.application.status()
            self._todos = self._status.todos
        except Exception as error:
            warnings.append(f"status: {type(error).__name__}")
        if self._context_status is not None:
            try:
                self._context_status = self.application.context_status()
                if self._context_status.warning:
                    warnings.append(self._context_status.warning)
            except Exception as error:
                warnings.append(f"context: {type(error).__name__}")
        self._status_warning = ", ".join(warnings)
        self._invalidate()

    def _cycle_collaboration_mode(self) -> None:
        cycle = getattr(self.application, "cycle_collaboration_mode", None)
        if cycle is None:
            return
        try:
            mode = cycle()
        except RuntimeError:
            return
        self._refresh_status()
        self._spawn(
            self._write(system_message(f"Collaboration mode: {mode.value.title()}"))
        )


class MyCodeTui:
    def __init__(self, application: ApplicationService) -> None:
        self.app = MyCodeApp(application)

    async def run(self) -> None:
        await self.app.run_async()


__all__ = ["MyCodeApp", "MyCodeTui"]
