"""Alternate-screen pager for the complete persisted conversation view."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol

from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import ANSI, FormattedText
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.layout import Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.output import Output
from rich.console import Console, Group, RenderableType
from rich.text import Text

from my_code.application.contracts.views import (
    TranscriptAttachment,
    TranscriptReasoning,
    TranscriptSummary,
    TranscriptText,
    TranscriptToolCall,
    TranscriptToolResult,
    TranscriptValue,
    TranscriptView,
)
from my_code.sessions.request_audit import ResolvedAuditRequest
from my_code.tui.terminal import (
    configure_key_timeouts,
    terminal_color_depth,
    terminal_output,
)
from my_code.tui.widgets import CodexMarkdown, reasoning_message, work_separator


class TranscriptSource(Protocol):
    def current_transcript_view(self) -> TranscriptView: ...


class TranscriptPager:
    """Read-only viewport that follows persisted entries while at the tail."""

    def __init__(
        self,
        source: TranscriptSource,
        *,
        input: Input | None = None,
        output: Output | None = None,
    ) -> None:
        self.source = source
        self._revision = -1
        self._lines: list[str] = []
        self._top = 0
        self._follow_tail = True
        self._closed = False
        self._width = -1
        self._view = TranscriptView(0, ())
        self._selected_request = 0
        self._request_detail = False
        self._watch_task: asyncio.Task[None] | None = None
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="my-code-transcript-render"
        )
        self._executor_closed = False
        bindings = self._bindings()
        resolved_output = terminal_output(output)
        self.application: Application[None] = Application(
            layout=Layout(
                Window(
                    FormattedTextControl(self._visible_text),
                    wrap_lines=False,
                    always_hide_cursor=True,
                )
            ),
            key_bindings=bindings,
            full_screen=True,
            mouse_support=False,
            input=input,
            output=resolved_output,
            color_depth=terminal_color_depth(resolved_output),
            refresh_interval=0.25,
        )
        configure_key_timeouts(self.application)

    async def run_async(self) -> None:
        await self._refresh_async()
        self._watch_task = asyncio.create_task(self._watch())
        try:
            await self.application.run_async()
        finally:
            self._closed = True
            self._watch_task.cancel()
            await asyncio.gather(self._watch_task, return_exceptions=True)
            self._shutdown_executor()

    def close(self) -> None:
        self._closed = True
        if self.application.is_running:
            self.application.exit()
        elif self._watch_task is None:
            self._shutdown_executor()

    def _shutdown_executor(self) -> None:
        if self._executor_closed:
            return
        self._executor_closed = True
        self._executor.shutdown(wait=True, cancel_futures=True)

    async def _watch(self) -> None:
        while not self._closed:
            await asyncio.sleep(0.1)
            await self._refresh_async()

    def _bindings(self) -> KeyBindings:
        bindings = KeyBindings()

        @bindings.add("up")
        def up(event: KeyPressEvent) -> None:
            del event
            self.scroll(-1)

        @bindings.add("down")
        def down(event: KeyPressEvent) -> None:
            del event
            self.scroll(1)

        @bindings.add("pageup")
        def page_up(event: KeyPressEvent) -> None:
            del event
            self.scroll(-self._page_height())

        @bindings.add("pagedown")
        def page_down(event: KeyPressEvent) -> None:
            del event
            self.scroll(self._page_height())

        @bindings.add("home")
        def home(event: KeyPressEvent) -> None:
            del event
            self._top = 0
            self._follow_tail = False
            self.application.invalidate()

        @bindings.add("end")
        def end(event: KeyPressEvent) -> None:
            del event
            self._follow_tail = True
            self._top = self._max_top()
            self.application.invalidate()

        @bindings.add("c-t")
        @bindings.add("q")
        def quit_pager(event: KeyPressEvent) -> None:
            event.app.exit()

        @bindings.add("escape")
        def escape(event: KeyPressEvent) -> None:
            if self._request_detail:
                self._request_detail = False
                self._rebuild_lines()
            else:
                event.app.exit()

        @bindings.add("n")
        def next_request(event: KeyPressEvent) -> None:
            del event
            self._move_request(1)

        @bindings.add("N")
        def previous_request(event: KeyPressEvent) -> None:
            del event
            self._move_request(-1)

        @bindings.add("enter")
        def open_request(event: KeyPressEvent) -> None:
            del event
            if self._view.requests:
                self._request_detail = True
                self._rebuild_lines()

        return bindings

    def scroll(self, offset: int) -> None:
        self._top = min(self._max_top(), max(0, self._top + offset))
        self._follow_tail = self._top == self._max_top()
        self.application.invalidate()

    def _refresh(self) -> None:
        """Synchronous compatibility hook; the live pager uses `_refresh_async`."""

        if self._closed:
            return
        view = self.source.current_transcript_view()
        width = max(20, self.application.output.get_size().columns)
        if view.revision == self._revision and width == self._width:
            return
        initial = self._revision == -1
        self._revision = view.revision
        self._view = view
        self._width = width
        self._selected_request = (
            max(0, len(view.requests) - 1)
            if initial
            else min(self._selected_request, max(0, len(view.requests) - 1))
        )
        self._lines = (
            _render_renderable_lines(
                request_audit_renderable(view.requests[self._selected_request]), width
            )
            if self._request_detail and view.requests
            else _render_lines(view, width)
        )
        if self._follow_tail:
            self._top = self._max_top()
        else:
            self._top = min(self._top, self._max_top())

    async def _refresh_async(self) -> None:
        if self._closed:
            return
        width = max(20, self.application.output.get_size().columns)
        snapshots: list[tuple[TranscriptView, list[str]]] = []
        errors: list[BaseException] = []

        def prepare() -> None:
            try:
                view = self.source.current_transcript_view()
                if view.revision == self._revision and width == self._width:
                    return
                renderable = (
                    request_audit_renderable(view.requests[self._selected_request])
                    if self._request_detail and view.requests
                    else transcript_renderable(view)
                )
                snapshots.append((view, _render_renderable_lines(renderable, width)))
            except BaseException as error:
                errors.append(error)

        await asyncio.get_running_loop().run_in_executor(self._executor, prepare)
        if errors:
            raise errors[0]
        if not snapshots or self._closed:
            return
        view, lines = snapshots[0]
        initial = self._revision == -1
        self._revision = view.revision
        self._view = view
        self._selected_request = (
            max(0, len(view.requests) - 1)
            if initial
            else min(self._selected_request, max(0, len(view.requests) - 1))
        )
        self._width = width
        self._lines = lines
        if self._follow_tail:
            self._top = self._max_top()
        else:
            self._top = min(self._top, self._max_top())
        self.application.invalidate()

    def _page_height(self) -> int:
        return max(1, self.application.output.get_size().rows - 1)

    def _max_top(self) -> int:
        return max(0, len(self._lines) - self._page_height())

    def _visible_text(self) -> ANSI | FormattedText:
        page_height = self._page_height()
        body = self._lines[self._top : self._top + page_height]
        request_position = (
            f" · request {self._selected_request + 1}/{len(self._view.requests)}"
            if self._view.requests
            else ""
        )
        mode = "Request detail" if self._request_detail else "Transcript"
        footer = (
            f"{mode}{request_position} · ↑↓ PgUp/PgDn Home/End · "
            "n/N request · Enter detail · Ctrl+T/q close"
        )
        return ANSI("\n".join((*body, f"\x1b[2m{footer}\x1b[0m")))

    def _move_request(self, offset: int) -> None:
        if not self._view.requests:
            return
        self._selected_request = min(
            len(self._view.requests) - 1,
            max(0, self._selected_request + offset),
        )
        self._rebuild_lines()

    def _rebuild_lines(self) -> None:
        width = max(20, self.application.output.get_size().columns)
        if self._request_detail and self._view.requests:
            renderable = request_audit_renderable(
                self._view.requests[self._selected_request]
            )
            self._lines = _render_renderable_lines(renderable, width)
        else:
            self._lines = _render_lines(self._view, width)
        if self._request_detail:
            self._top = 0
        else:
            needle = f"Model request #{self._selected_request + 1}"
            self._top = min(
                self._max_top(),
                next(
                    (index for index, line in enumerate(self._lines) if needle in line),
                    self._max_top(),
                ),
            )
        self._follow_tail = False
        self.application.invalidate()


def transcript_renderable(view: TranscriptView) -> RenderableType:
    blocks: list[RenderableType] = []
    if view.audit_legacy_missing:
        blocks.append(
            Text(
                "Request audit gap · this legacy session predates exact "
                "request auditing.",
                style="bold yellow",
            )
        )
    seen_prompt_refs: set[str] = set()
    seen_tool_refs: set[str] = set()
    for request in view.requests:
        manifest = request.manifest
        lines = [
            f"purpose: {manifest.purpose.value}",
            f"status: {manifest.status}",
            f"causal head: {manifest.causal_head or '(none)'}",
            f"step/attempt: {manifest.step}/{manifest.attempt}",
            f"input manifest: {len(manifest.input_refs)} ordered items",
            f"max output: {manifest.max_output_tokens}",
            f"reasoning: {manifest.reasoning_mode}",
        ]
        new_prompts = tuple(
            value
            for ref, value in zip(
                manifest.system_prompt_refs,
                request.system_prompt_sections,
                strict=True,
            )
            if ref not in seen_prompt_refs
        )
        new_tools = tuple(
            value
            for ref, value in zip(manifest.tool_refs, request.tools, strict=True)
            if ref not in seen_tool_refs
        )
        seen_prompt_refs.update(manifest.system_prompt_refs)
        seen_tool_refs.update(manifest.tool_refs)
        blocks.append(
            Group(
                Text(
                    f"Model request #{manifest.request_number} · {manifest.request_id}",
                    style="bold magenta",
                ),
                Text("\n".join(lines)),
                *(
                    (
                        Text("New system prompt sections", style="bold"),
                        _json_text(new_prompts),
                    )
                    if new_prompts
                    else (Text("System prompt · unchanged references", style="dim"),)
                ),
                *(
                    (Text("Tool catalog changes", style="bold"), _json_text(new_tools))
                    if new_tools
                    else (Text("Tool catalog · unchanged references", style="dim"),)
                ),
            )
        )
    work_visible = False
    for entry in view.entries:
        if isinstance(entry, TranscriptText):
            if entry.role == "user":
                work_visible = False
            elif entry.is_final_answer and work_visible:
                blocks.append(work_separator())
                work_visible = False
            elif entry.role == "assistant" and not entry.is_final_answer:
                work_visible = True
            label = "› User" if entry.role == "user" else "Assistant"
            blocks.append(
                Group(Text(label, style="bold cyan"), CodexMarkdown(entry.text))
            )
        elif isinstance(entry, TranscriptReasoning):
            blocks.append(reasoning_message(entry.presentation))
            work_visible = True
        elif isinstance(entry, TranscriptToolCall):
            blocks.append(
                Group(
                    Text(f"Tool call · {entry.name}", style="bold"),
                    _value_text(entry.input),
                )
            )
            work_visible = True
        elif isinstance(entry, TranscriptToolResult):
            style = "bold red" if entry.is_error else "bold green"
            blocks.append(
                Group(
                    Text(f"Tool result · {entry.name}", style=style),
                    Text(entry.content),
                )
            )
            work_visible = True
        elif isinstance(entry, TranscriptSummary):
            blocks.append(
                Group(
                    Text("Conversation summary", style="bold yellow"),
                    CodexMarkdown(entry.content),
                )
            )
        elif isinstance(entry, TranscriptAttachment):
            blocks.append(
                Group(
                    Text(f"Attachment · {entry.attachment_kind}", style="bold"),
                    _value_text(entry.value),
                )
            )
    spaced: list[RenderableType] = []
    for block in blocks:
        if spaced:
            spaced.append(Text())
        spaced.append(block)
    return (
        Group(*spaced)
        if spaced
        else Text("No persisted conversation yet.", style="dim")
    )


def _value_text(value: TranscriptValue) -> Text:
    lines: list[str] = []
    _append_value(lines, value, "")
    return Text("\n".join(lines) or "(empty)")


def _append_value(lines: list[str], value: TranscriptValue, indent: str) -> None:
    if value.kind == "scalar":
        lines.append(f"{indent}{value.scalar}")
    elif value.kind == "object":
        if not value.fields:
            lines.append(f"{indent}{{}}")
        for field in value.fields:
            if field.value.kind == "scalar":
                lines.append(f"{indent}{field.key}: {field.value.scalar}")
            else:
                lines.append(f"{indent}{field.key}:")
                _append_value(lines, field.value, indent + "  ")
    else:
        if not value.items:
            lines.append(f"{indent}[]")
        for item in value.items:
            if item.kind == "scalar":
                lines.append(f"{indent}- {item.scalar}")
            else:
                lines.append(f"{indent}-")
                _append_value(lines, item, indent + "  ")


def _render_lines(view: TranscriptView, width: int) -> list[str]:
    return _render_renderable_lines(transcript_renderable(view), width)


def _render_renderable_lines(renderable: RenderableType, width: int) -> list[str]:
    from io import StringIO

    stream = StringIO()
    console = Console(
        file=stream,
        width=width,
        force_terminal=True,
        color_system="truecolor",
    )
    console.print(renderable, end="")
    return stream.getvalue().splitlines()


def request_audit_renderable(request: ResolvedAuditRequest) -> RenderableType:
    manifest = request.manifest
    origins = tuple(
        {
            "position": index,
            "kind": origin.kind.value,
            "source_id": origin.source_id,
            "source": origin.source,
            "attachment_kind": origin.attachment_kind,
            "input": value,
        }
        for index, (origin, value) in enumerate(
            zip(manifest.origins, request.input, strict=True), 1
        )
    )
    return Group(
        Text(
            f"Resolved model request #{manifest.request_number} · "
            f"{manifest.request_id}",
            style="bold magenta",
        ),
        Text(
            "\n".join(
                (
                    f"purpose: {manifest.purpose.value}",
                    f"status: {manifest.status}",
                    f"error: {manifest.error or '(none)'}",
                    f"causal head: {manifest.causal_head or '(none)'}",
                    f"step/attempt: {manifest.step}/{manifest.attempt}",
                    f"compact trigger: {manifest.compact_trigger or '(none)'}",
                    f"max output tokens: {manifest.max_output_tokens}",
                    f"reasoning mode: {manifest.reasoning_mode}",
                )
            )
        ),
        Text("System prompt sections", style="bold"),
        _json_text(request.system_prompt_sections),
        Text("Ordered model input and origins", style="bold"),
        _json_text(origins),
        Text("Tool definitions and JSON Schema", style="bold"),
        _json_text(request.tools),
        Text("Context budget", style="bold"),
        _json_text(manifest.budget or {}),
    )


def _json_text(value: object) -> Text:
    import json

    return Text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


__all__ = [
    "TranscriptPager",
    "request_audit_renderable",
    "transcript_renderable",
]
