"""Alternate-screen pager for the complete persisted conversation view."""

from __future__ import annotations

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

from my_code.chat.views import (
    TranscriptAttachment,
    TranscriptReasoning,
    TranscriptSummary,
    TranscriptText,
    TranscriptToolCall,
    TranscriptToolResult,
    TranscriptValue,
    TranscriptView,
)
from my_code.tui.terminal import terminal_color_depth, terminal_output
from my_code.tui.widgets import CodexMarkdown, reasoning_message


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

    async def run_async(self) -> None:
        self._refresh()
        await self.application.run_async()

    def close(self) -> None:
        self._closed = True
        if self.application.is_running:
            self.application.exit()

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
        @bindings.add("escape")
        @bindings.add("q")
        def quit_pager(event: KeyPressEvent) -> None:
            event.app.exit()

        return bindings

    def scroll(self, offset: int) -> None:
        self._refresh()
        self._top = min(self._max_top(), max(0, self._top + offset))
        self._follow_tail = self._top == self._max_top()
        self.application.invalidate()

    def _refresh(self) -> None:
        if self._closed:
            return
        view = self.source.current_transcript_view()
        if view.revision == self._revision:
            return
        self._revision = view.revision
        width = max(20, self.application.output.get_size().columns)
        self._lines = _render_lines(view, width)
        if self._follow_tail:
            self._top = self._max_top()
        else:
            self._top = min(self._top, self._max_top())

    def _page_height(self) -> int:
        return max(1, self.application.output.get_size().rows - 1)

    def _max_top(self) -> int:
        return max(0, len(self._lines) - self._page_height())

    def _visible_text(self) -> ANSI | FormattedText:
        self._refresh()
        page_height = self._page_height()
        body = self._lines[self._top : self._top + page_height]
        footer = "Transcript · ↑↓ PgUp/PgDn Home/End · Ctrl+T/Esc/q close"
        return ANSI("\n".join((*body, f"\x1b[2m{footer}\x1b[0m")))


def transcript_renderable(view: TranscriptView) -> RenderableType:
    blocks: list[RenderableType] = []
    for entry in view.entries:
        if isinstance(entry, TranscriptText):
            label = "› User" if entry.role == "user" else "Assistant"
            blocks.append(
                Group(Text(label, style="bold cyan"), CodexMarkdown(entry.text))
            )
        elif isinstance(entry, TranscriptReasoning):
            blocks.append(reasoning_message(entry.presentation))
        elif isinstance(entry, TranscriptToolCall):
            blocks.append(
                Group(
                    Text(f"Tool call · {entry.name}", style="bold"),
                    _value_text(entry.input),
                )
            )
        elif isinstance(entry, TranscriptToolResult):
            style = "bold red" if entry.is_error else "bold green"
            blocks.append(
                Group(
                    Text(f"Tool result · {entry.name}", style=style),
                    Text(entry.content),
                )
            )
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
    from io import StringIO

    stream = StringIO()
    console = Console(
        file=stream,
        width=width,
        force_terminal=True,
        color_system="truecolor",
    )
    console.print(transcript_renderable(view), end="")
    return stream.getvalue().splitlines()


__all__ = ["TranscriptPager", "transcript_renderable"]
