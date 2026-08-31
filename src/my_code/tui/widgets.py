"""Rich renderables for the native scrollback terminal host."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Sequence
from difflib import SequenceMatcher
from functools import lru_cache
from io import StringIO
from typing import Literal, Protocol

from prompt_toolkit.formatted_text import ANSI
from rich import box
from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
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

from my_code.chat.history import (
    HistoryContextGroup,
    HistoryEntry,
    HistoryPlan,
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
from my_code.version import __version__


class FileDiffLineView(Protocol):
    @property
    def kind(self) -> Literal["context", "addition", "deletion", "omitted"]: ...

    @property
    def text(self) -> str: ...

    @property
    def old_line(self) -> int | None: ...

    @property
    def new_line(self) -> int | None: ...

    @property
    def omitted_lines(self) -> int: ...


class FileDiffHunkView(Protocol):
    @property
    def old_start(self) -> int: ...

    @property
    def old_count(self) -> int: ...

    @property
    def new_start(self) -> int: ...

    @property
    def new_count(self) -> int: ...

    @property
    def lines(self) -> tuple[FileDiffLineView, ...]: ...


class FileDiffPresentationView(Protocol):
    @property
    def path(self) -> str: ...

    @property
    def operation(self) -> Literal["created", "updated"]: ...

    @property
    def additions(self) -> int: ...

    @property
    def deletions(self) -> int: ...

    @property
    def hunks(self) -> tuple[FileDiffHunkView, ...]: ...

    @property
    def old_ends_with_newline(self) -> bool: ...

    @property
    def new_ends_with_newline(self) -> bool: ...

    @property
    def omitted_lines(self) -> int: ...

    @property
    def omitted_reason(self) -> str | None: ...


class ToolResultPresentationView(Protocol):
    @property
    def summary(self) -> str: ...

    @property
    def detail(self) -> str | None: ...

    @property
    def file_diff(self) -> FileDiffPresentationView | None: ...


class InjectedContextView(Protocol):
    @property
    def source(self) -> str: ...

    @property
    def attachment_kind(self) -> str | None: ...

    @property
    def text(self) -> str: ...


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
                f"{status.mcp_connected_count}/{status.mcp_server_count} MCP · "
                f"{status.execution_environment}",
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


def command_echo(command: str) -> Text:
    return Text(command, style="magenta")


def information_card(title: str, body: RenderableType) -> Panel:
    heading = Text.assemble((">_ ", "bold bright_black"), (title, "bold"))
    return Panel(
        Group(heading, Text(), body),
        box=box.ROUNDED,
        border_style="bright_black",
        padding=(1, 2),
        expand=True,
    )


def field_table(rows: tuple[tuple[str, RenderableType | str], ...]) -> Table:
    table = Table.grid(padding=(0, 2), expand=False)
    table.add_column(style="bright_black", justify="right", no_wrap=True)
    table.add_column(ratio=1, overflow="fold")
    for label, value in rows:
        table.add_row(f"{label}:", value)
    return table


class _BlockSeparator:
    def __init__(self, label: str | None = None) -> None:
        self.label = label

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        del console
        if self.label:
            prefix = f"─ {self.label} "
            line = prefix + "─" * max(0, options.max_width - len(prefix))
        else:
            line = "─" * options.max_width
        yield Text(line, style="bright_black")


def block_separator(label: str | None = None) -> RenderableType:
    """Return the shared full-width divider for top-level semantic blocks."""

    return _BlockSeparator(label)


def work_separator() -> RenderableType:
    """Compatibility name for an unlabeled semantic block divider."""

    return block_separator()


def injected_context_message(
    request_number: int, items: Sequence[InjectedContextView]
) -> RenderableType:
    blocks: list[RenderableType] = [
        block_separator(f"Injected context · request #{request_number}")
    ]
    for index, item in enumerate(items):
        if index:
            blocks.append(block_separator())
        label = item.source
        if item.attachment_kind:
            label += f" · {item.attachment_kind}"
        blocks.extend(
            (
                Text(label, style="bold bright_black"),
                Text(_bounded_injection(item.text), style="dim"),
            )
        )
    blocks.append(block_separator())
    return Group(*blocks)


def detailed_tool_call_message(name: str, input: object) -> RenderableType:
    payload = json.dumps(input, ensure_ascii=False, indent=2, sort_keys=True)
    return Group(
        block_separator(f"Tool call · {name}"),
        Text(payload, style="dim"),
    )


def _bounded_injection(value: str) -> str:
    lines = value.splitlines()
    omitted_lines = max(0, len(lines) - 60)
    if omitted_lines:
        lines = [*lines[:48], f"[… {omitted_lines} lines omitted …]", *lines[-12:]]
    rendered = "\n".join(lines)
    if len(rendered.encode("utf-8")) <= 8192:
        return rendered
    encoded = rendered.encode("utf-8")
    head = encoded[:6144].decode("utf-8", errors="ignore")
    tail = encoded[-1536:].decode("utf-8", errors="ignore")
    omitted_chars = max(0, len(rendered) - len(head) - len(tail))
    return f"{head}\n[… {omitted_chars} characters omitted …]\n{tail}"


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
) -> RenderableType:
    marker = "×" if is_error else "✓"
    line = Text.assemble(
        (f"{marker} ", "bold red" if is_error else "bold green"),
        (use.display_name, "bold"),
        (f"({use.summary})", "dim"),
        (f"\n  ⎿ {result.summary}", "red" if is_error else "dim"),
    )
    if is_error and result.detail:
        line.append(f"\n     {result.detail}", style="red")
    if not is_error and result.file_diff is not None:
        return Group(line, file_diff_message(result.file_diff))
    return line


def tool_activity_message(
    activity: ToolActivityGroup,
    *,
    tail: int | None = None,
    expand_diffs: bool = True,
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
        renderables.append(_tool_activity_item(item, expand_diff=expand_diffs))
    return Group(*renderables)


def _tool_activity_item(item: ToolActivityItem, *, expand_diff: bool) -> RenderableType:
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
    diff = getattr(item.result, "file_diff", None)
    if expand_diff and not item.is_error and diff is not None:
        return Group(text, Padding(file_diff_message(diff), (0, 0, 0, 4)))
    return text


def file_diff_message(
    diff: FileDiffPresentationView, theme: TuiTheme | None = None
) -> RenderableType:
    """Render a persisted file diff without consulting the filesystem."""

    theme = theme or TuiTheme.detect()
    verb = "Created" if diff.operation == "created" else "Edited"
    title = (
        Text(f"{verb} {diff.path} (diff omitted)", style="bold")
        if diff.omitted_reason is not None
        else Text.assemble(
            (f"{verb} {diff.path} ", "bold"),
            (f"(+{diff.additions}", "green"),
            (f" -{diff.deletions})", "red"),
        )
    )
    if diff.omitted_reason is not None:
        return Group(title, Text(f"  … {diff.omitted_reason}", style="dim italic"))
    if not diff.hunks:
        detail = "No content changes"
        if diff.old_ends_with_newline != diff.new_ends_with_newline:
            detail = "End-of-file newline changed"
        return Group(title, Text(f"  {detail}", style="dim"))

    dark = theme.palette.background is None or (sum(theme.palette.background) < 3 * 160)
    colors = (
        ("#123d27", "#1f633d", "#4a1f24", "#762d38")
        if dark
        else ("#d9f2df", "#a8dfb5", "#f6dadd", "#ebb0b7")
    )
    max_line = max(
        (
            number
            for hunk in diff.hunks
            for line in hunk.lines
            for number in (line.old_line, line.new_line)
            if number is not None
        ),
        default=1,
    )
    width = len(str(max_line))
    language = _diff_language(diff.path)
    blocks: list[RenderableType] = [title]
    for index, hunk in enumerate(diff.hunks):
        if index:
            blocks.append(Text("  …", style="dim"))
        table = Table.grid(expand=True, padding=0)
        table.add_column(width=width * 2 + 4, no_wrap=True)
        table.add_column(ratio=1, overflow="fold")
        word_ranges = _word_diff_ranges(hunk.lines)
        for line_index, line in enumerate(hunk.lines):
            if line.kind == "omitted":
                table.add_row(
                    Text("…".rjust(width * 2 + 3), style="dim"),
                    Text(
                        f"{line.omitted_lines} unchanged/change line(s) omitted", "dim"
                    ),
                )
                continue
            safe = _safe_diff_text(line.text)
            marker = {"addition": "+", "deletion": "-", "context": " "}[line.kind]
            old = "" if line.old_line is None else str(line.old_line)
            new = "" if line.new_line is None else str(line.new_line)
            gutter = Text(
                f"{old:>{width}} {new:>{width}} {marker} ",
                style=(
                    "green"
                    if line.kind == "addition"
                    else "red"
                    if line.kind == "deletion"
                    else "dim"
                ),
            )
            code = Syntax(
                safe,
                language,
                theme="ansi_dark" if dark else "ansi_light",
                background_color="default",
            ).highlight(safe)
            code.rstrip()
            if line.kind == "context":
                code.stylize("dim")
                row_style = None
            else:
                background, deep = colors[:2] if line.kind == "addition" else colors[2:]
                row_style = f"on {background}"
                code.stylize(row_style)
                for start, end in word_ranges.get(line_index, ()):
                    code.stylize(f"on {deep}", start, end)
            table.add_row(gutter, code, style=row_style)
        blocks.append(table)
    if diff.old_ends_with_newline != diff.new_ends_with_newline:
        state = (
            "Added newline at end of file"
            if diff.new_ends_with_newline
            else "No newline at end of file"
        )
        blocks.append(Text(f"  {state}", style="dim italic"))
    if diff.omitted_lines:
        blocks.append(
            Text(f"  … {diff.omitted_lines} diff line(s) omitted", style="dim italic")
        )
    return Group(*blocks)


def _diff_language(path: str) -> str:
    try:
        return Syntax.guess_lexer(path, "")
    except Exception:
        return "text"


def _safe_diff_text(value: str) -> str:
    expanded = value.expandtabs(4)
    return "".join(
        char
        if char == " " or not unicodedata.category(char).startswith("C")
        else f"\\x{ord(char):02x}"
        for char in expanded
    )


def _word_diff_ranges(
    lines: tuple[FileDiffLineView, ...],
) -> dict[int, tuple[tuple[int, int], ...]]:
    ranges: dict[int, tuple[tuple[int, int], ...]] = {}
    index = 0
    while index < len(lines):
        if lines[index].kind != "deletion":
            index += 1
            continue
        deleted_start = index
        while index < len(lines) and lines[index].kind == "deletion":
            index += 1
        added_start = index
        while index < len(lines) and lines[index].kind == "addition":
            index += 1
        for offset in range(min(added_start - deleted_start, index - added_start)):
            old_index = deleted_start + offset
            new_index = added_start + offset
            old = _safe_diff_text(lines[old_index].text)
            new = _safe_diff_text(lines[new_index].text)
            old_tokens = _word_tokens(old)
            new_tokens = _word_tokens(new)
            matcher = SequenceMatcher(
                None,
                [token for token, _, _ in old_tokens],
                [token for token, _, _ in new_tokens],
                autojunk=False,
            )
            if 1.0 - matcher.ratio() > 0.4:
                continue
            old_ranges: list[tuple[int, int]] = []
            new_ranges: list[tuple[int, int]] = []
            for tag, i1, i2, j1, j2 in matcher.get_opcodes():
                if tag == "equal":
                    continue
                if i1 < i2:
                    old_ranges.append((old_tokens[i1][1], old_tokens[i2 - 1][2]))
                if j1 < j2:
                    new_ranges.append((new_tokens[j1][1], new_tokens[j2 - 1][2]))
            ranges[old_index] = tuple(old_ranges)
            ranges[new_index] = tuple(new_ranges)
    return ranges


def _word_tokens(value: str) -> list[tuple[str, int, int]]:
    return [
        (match.group(), match.start(), match.end())
        for match in re.finditer(r"\s+|\w+|[^\w\s]+", value)
    ]


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
    if isinstance(entry, HistoryPlan):
        return Group(
            Text("Proposed plan", style="bold cyan"), assistant_message(entry.plan)
        )
    if isinstance(entry, HistoryContextGroup):
        return injected_context_message(entry.request_number, entry.items)
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


def status_line(status: RuntimeStatus, context_usage: str | None) -> str:
    line = f"{status.model} · {status.context_entry_count} context entries"
    return line if context_usage is None else f"{line}    {context_usage}"


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
    "block_separator",
    "CodexMarkdown",
    "capability_table",
    "command_echo",
    "detailed_tool_call_message",
    "field_table",
    "file_diff_message",
    "history_message",
    "information_card",
    "injected_context_message",
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
    "work_separator",
]
