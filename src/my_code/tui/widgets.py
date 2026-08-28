"""Rich renderables for the native scrollback terminal host."""

from __future__ import annotations

import re
from functools import lru_cache
from io import StringIO
from typing import Protocol

from prompt_toolkit.formatted_text import ANSI
from rich import box
from rich.console import Console, Group, RenderableType
from rich.markdown import (
    BlockQuote,
    CodeBlock,
    Heading,
    HorizontalRule,
    Markdown,
    TableElement,
)
from rich.padding import Padding
from rich.panel import Panel
from rich.segment import Segment
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from my_code import __version__
from my_code.chat.history import (
    HistoryEntry,
    HistoryReasoning,
    HistoryText,
    HistoryToolCall,
)
from my_code.chat.status import RuntimeStatus
from my_code.features.todos.models import TodoItem
from my_code.model.primitives import ReasoningPresentation
from my_code.tools.presentation import ToolUsePresentation
from my_code.tui.activity import ToolActivityGroup, ToolActivityItem
from my_code.tui.dimensions import SURFACE_VERTICAL_PADDING
from my_code.tui.theme import TuiTheme


class ToolResultPresentationView(Protocol):
    @property
    def summary(self) -> str: ...

    @property
    def detail(self) -> str | None: ...


def welcome(status: RuntimeStatus, theme: TuiTheme | None = None) -> RenderableType:
    theme = theme or TuiTheme.detect()
    wordmark = Text.assemble(
        ("›_", "bold cyan"),
        ("  my-code", "bold italic"),
        (f" v{__version__}", "dim"),
    )
    details = Group(
        wordmark,
        Text.assemble(
            (status.provider_id, "bold cyan"),
            (" / ", "dim"),
            (status.model, ""),
            (f"  ·  {status.credential_source}", "dim"),
        ),
        Text.assemble(
            (status.cwd, "dim"),
            (
                f"\n{status.tool_count} tools · {status.skill_count} skills · "
                f"{status.mcp_connected_count}/{status.mcp_server_count} MCP",
                "dim",
            ),
        ),
        Text("Type / for commands · @ for files", style="dim"),
    )
    return Panel.fit(details, border_style="bright_black", padding=(0, 2))


def user_message(prompt: str, theme: TuiTheme | None = None) -> RenderableType:
    theme = theme or TuiTheme.detect()
    content = Text.assemble(("› ", "bold cyan"), prompt)
    return Padding(
        content,
        (SURFACE_VERTICAL_PADDING, 1),
        style=theme.rich_surface,
        expand=True,
    )


class _CodexHeading(Heading):
    def __rich_console__(self, console: Console, options: object):  # type: ignore[override]
        text = Text(f"{'#' * int(self.tag[1:])} ", style=self.style_name)
        text.append(self.text)
        yield text


class _CodexCodeBlock(CodeBlock):
    def __rich_console__(self, console: Console, options: object):  # type: ignore[override]
        yield Syntax(
            str(self.text).rstrip(),
            self.lexer_name,
            theme="ansi_dark",
            word_wrap=True,
            padding=0,
            background_color="default",
        )


class _CodexBlockQuote(BlockQuote):
    def __rich_console__(self, console: Console, options):  # type: ignore[no-untyped-def, override]
        render_options = options.update(width=max(1, options.max_width - 2))
        lines = console.render_lines(self.elements, render_options, style=self.style)
        for line in lines:
            yield Segment("> ", console.get_style("green"))
            yield from line
            yield Segment("\n")


class _CodexHorizontalRule(HorizontalRule):
    def __rich_console__(self, console: Console, options: object):  # type: ignore[override]
        del console, options
        yield Text("———", style="bright_black")


class _CodexTable(TableElement):
    def __rich_console__(self, console: Console, options: object):  # type: ignore[override]
        del console, options
        table = Table(
            box=box.SIMPLE_HEAD,
            pad_edge=False,
            show_edge=False,
            collapse_padding=True,
        )
        if self.header is not None and self.header.row is not None:
            for column in self.header.row.cells:
                table.add_column(column.content.copy(), style="bold")
        if self.body is not None:
            for row in self.body.rows:
                table.add_row(*(element.content for element in row.cells))
        yield table


class CodexMarkdown(Markdown):
    """Rich Markdown with terminal-native Codex visual semantics."""

    elements = {
        **Markdown.elements,
        "heading_open": _CodexHeading,
        "fence": _CodexCodeBlock,
        "code_block": _CodexCodeBlock,
        "blockquote_open": _CodexBlockQuote,
        "hr": _CodexHorizontalRule,
        "table_open": _CodexTable,
    }
    _theme = Theme(
        {
            "markdown.h1": "bold underline",
            "markdown.h2": "bold",
            "markdown.h3": "bold italic",
            "markdown.h4": "bold",
            "markdown.h5": "italic",
            "markdown.h6": "dim italic",
            "markdown.code": "cyan",
            "markdown.code_block": "none",
            "markdown.link": "cyan underline",
            "markdown.link_url": "cyan underline",
            "markdown.item.bullet": "bright_black",
            "markdown.item.number": "bright_black",
            "markdown.table.border": "bright_black",
        },
        inherit=True,
    )

    def __init__(self, markup: str) -> None:
        super().__init__(markup, code_theme="ansi_dark")

    def __rich_console__(self, console: Console, options):  # type: ignore[no-untyped-def, override]
        with console.use_theme(self._theme):
            yield from super().__rich_console__(console, options)


def assistant_message(content: str) -> CodexMarkdown:
    return CodexMarkdown(content or "<no text response>")


@lru_cache(maxsize=8)
def streaming_assistant_message(content: str, width: int) -> ANSI:
    """Render partial Markdown for prompt_toolkit's transient live region."""

    return streaming_renderable(assistant_message(content), width)


def streaming_renderable(renderable: RenderableType, width: int) -> ANSI:
    """Render a Rich value into prompt_toolkit's transient live region."""

    stream = StringIO()
    console = Console(
        file=stream,
        width=max(width, 20),
        force_terminal=True,
        color_system="truecolor",
    )
    console.print(renderable, end="")
    rendered = "\n".join(line.rstrip() for line in stream.getvalue().splitlines())
    return ANSI(rendered)


def system_message(content: str, *, error: bool = False) -> Text:
    return Text(content, style="red" if error else "dim")


def reasoning_message(presentation: ReasoningPresentation) -> RenderableType:
    if presentation.disclosure == "redacted":
        return Text("思考内容已由模型提供方隐藏", style="dim italic")
    if presentation.disclosure == "hidden":
        return Text("模型已完成思考", style="dim italic")
    body = _bounded_reasoning("\n\n".join(presentation.parts))
    title = "思考过程" if presentation.disclosure == "verbatim" else "思考摘要"
    return Group(Text(title, style="dim italic"), CodexMarkdown(body))


def tool_message(
    use: ToolUsePresentation,
    result: ToolResultPresentationView,
    *,
    is_error: bool,
) -> Text:
    marker = "×" if is_error else "✓"
    line = Text.assemble(
        (f"{marker} ", "bold red" if is_error else "bold green"),
        (use.display_name, "bold"),
        (f"({use.summary})", "dim"),
        (f"\n  ⎿ {result.summary}", "red" if is_error else "dim"),
    )
    if is_error and result.detail:
        line.append(f"\n     {result.detail}", style="red")
    return line


def tool_activity_message(
    activity: ToolActivityGroup, *, tail: int | None = None
) -> RenderableType:
    items = activity.items[-tail:] if tail is not None else activity.items
    renderables: list[RenderableType] = []
    previous_category: str | None = None
    labels = {
        "explore": "Explored",
        "command": "Ran commands",
        "change": "Changed files",
        "other": "Called tools",
    }
    for item in items:
        if item.use.category != previous_category:
            renderables.append(Text(f"• {labels[item.use.category]}", style="bold"))
            previous_category = item.use.category
        renderables.append(_tool_activity_item(item))
    return Group(*renderables)


def _tool_activity_item(item: ToolActivityItem) -> Text:
    if item.running:
        marker, marker_style = "·", "bright_black"
    elif item.is_error:
        marker, marker_style = "×", "bold red"
    else:
        marker, marker_style = "✔", "green"
    text = Text.assemble(
        (f"  {marker} ", marker_style),
        (item.use.display_name, "bold"),
        (f" {item.use.summary}", "dim"),
    )
    if item.result is not None:
        text.append(f"\n    {item.result.summary}", "red" if item.is_error else "dim")
        if item.is_error and item.result.detail:
            text.append(f"\n    {item.result.detail}", "red")
    return text


def history_message(
    entry: HistoryEntry, theme: TuiTheme | None = None
) -> RenderableType:
    """Render both canonical and child-agent history through one entry point."""

    if isinstance(entry, HistoryText):
        if entry.role == "user":
            return user_message(entry.text, theme)
        if entry.role == "assistant":
            return assistant_message(entry.text)
        return system_message(entry.text)
    if isinstance(entry, HistoryReasoning):
        return reasoning_message(entry.presentation)
    assert isinstance(entry, HistoryToolCall)
    return tool_message(entry.use, entry.result, is_error=entry.is_error)


def todo_snapshot(todos: tuple[TodoItem, ...]) -> RenderableType:
    lines: list[Text] = [Text("• Updated Plan", style="bold")]
    if not todos:
        lines.append(Text("  (no tasks)", style="dim"))
    for todo in todos:
        if todo.status == "completed":
            lines.append(Text(f"  ✔ {todo.content}", style="green"))
        elif todo.status == "in_progress":
            lines.append(Text(f"  □ {todo.content}", style="bold cyan"))
        else:
            lines.append(Text(f"  □ {todo.content}", style="dim"))
    return Group(*lines)


def status_line(status: RuntimeStatus, context_usage: str) -> str:
    return (
        f"{status.model} · {status.permission_mode} · "
        f"{status.context_entry_count} context entries    {context_usage}"
    )


def capability_table(title: str, rows: tuple[tuple[str, ...], ...]) -> Table:
    table = Table(title=title, box=None, show_header=False, padding=(0, 1))
    width = max((len(row) for row in rows), default=1)
    for _ in range(width):
        table.add_column()
    for row in rows:
        table.add_row(*row)
    return table


def _bounded_reasoning(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value).strip()
    if len(normalized) <= 500:
        return normalized
    return normalized[:497].rstrip() + "…"


__all__ = [
    "assistant_message",
    "CodexMarkdown",
    "capability_table",
    "history_message",
    "reasoning_message",
    "status_line",
    "streaming_assistant_message",
    "streaming_renderable",
    "system_message",
    "todo_snapshot",
    "tool_activity_message",
    "tool_message",
    "user_message",
    "welcome",
]
