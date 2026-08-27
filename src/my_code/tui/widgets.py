"""Rich renderables for the native scrollback terminal host."""

from __future__ import annotations

import re
from functools import lru_cache
from io import StringIO
from typing import Protocol

from prompt_toolkit.formatted_text import ANSI
from rich.console import Console, Group, RenderableType
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

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
from my_code.tui.dimensions import SURFACE_VERTICAL_PADDING
from my_code.tui.theme import TuiTheme


class ToolResultPresentationView(Protocol):
    @property
    def summary(self) -> str: ...

    @property
    def detail(self) -> str | None: ...


def welcome(status: RuntimeStatus, theme: TuiTheme | None = None) -> RenderableType:
    theme = theme or TuiTheme.detect()
    logo = Text(
        " __  __ __   __  ____  ___  ____  _____\n"
        "|  \\/  |\\ \\ / / / ___|/ _ \\|  _ \\| ____|\n"
        "| |\\/| | \\ V / | |  | | | | | | |  _|\n"
        "| |  | |  | |  | |__| |_| | |_| | |___\n"
        "|_|  |_|  |_|   \\____|\\___/|____/|_____|",
        style="bold magenta",
    )
    details = Group(
        logo,
        Text.assemble(
            ("my-code", "bold"),
            (f" v{__version__}", "dim"),
            ("  ·  coding agent with explicit boundaries", "dim"),
        ),
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


def assistant_message(content: str) -> Markdown:
    return Markdown(content or "<no text response>")


@lru_cache(maxsize=8)
def streaming_assistant_message(content: str, width: int) -> ANSI:
    """Render partial Markdown for prompt_toolkit's transient live region."""

    stream = StringIO()
    console = Console(
        file=stream,
        width=max(width, 20),
        force_terminal=True,
        color_system="truecolor",
    )
    console.print(assistant_message(content), end="")
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
    return Group(Text(title, style="dim italic"), Markdown(body))


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


def todo_text(todos: tuple[TodoItem, ...], *, expanded: bool) -> str:
    if not todos:
        return ""
    pending = sum(todo.status == "pending" for todo in todos)
    active = sum(todo.status == "in_progress" for todo in todos)
    done = sum(todo.status == "completed" for todo in todos)
    lines = [f"Tasks  {active} in progress · {pending} pending · {done} done"]
    if expanded:
        for todo in todos:
            marker = {"completed": "✓", "in_progress": "●"}.get(todo.status, "○")
            lines.append(f"  {marker} {todo.content}")
    else:
        current = next(
            (todo.content for todo in todos if todo.status == "in_progress"), None
        )
        if current:
            lines.append(f"  ● {current}")
    return "\n".join(lines)


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
    "capability_table",
    "history_message",
    "reasoning_message",
    "status_line",
    "streaming_assistant_message",
    "system_message",
    "todo_text",
    "tool_message",
    "user_message",
    "welcome",
]
