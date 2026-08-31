"""Snapshots for Codex-style scrollback and transcript presentation."""

import re
from io import StringIO

from prompt_toolkit.formatted_text import fragment_list_to_text, to_formatted_text
from prompt_toolkit.output import DummyOutput
from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text

from my_code.application.contracts.history import HistoryContextItem
from my_code.application.contracts.status import ContextStatus, RuntimeStatus
from my_code.application.contracts.views import (
    TranscriptField,
    TranscriptReasoning,
    TranscriptText,
    TranscriptToolResult,
    TranscriptValue,
    TranscriptView,
)
from my_code.conversation.presentation import (
    FileDiffHunk,
    FileDiffLine,
    FileDiffPresentation,
    ToolResultPresentation,
)
from my_code.features.todos.models import TodoItem
from my_code.model.primitives import ReasoningPresentation
from my_code.tools.presentation import ToolUsePresentation
from my_code.tui.activity import ToolActivityGroup
from my_code.tui.block_flow import TurnBlockCoordinator
from my_code.tui.presentation import render_status_card
from my_code.tui.transcript import TranscriptPager, transcript_renderable
from my_code.tui.widgets import (
    assistant_message,
    file_diff_message,
    injected_context_message,
    streaming_assistant_message,
    streaming_renderable,
    todo_snapshot,
    tool_activity_message,
    welcome,
)


def _plain(renderable: object, *, width: int = 80) -> str:
    stream = StringIO()
    console = Console(file=stream, width=width, force_terminal=False)
    console.print(renderable)
    return "\n".join(line.rstrip() for line in stream.getvalue().splitlines())


def _ansi(renderable: object, *, width: int = 80) -> str:
    stream = StringIO()
    console = Console(
        file=stream,
        width=width,
        force_terminal=True,
        color_system="truecolor",
        no_color=False,
    )
    console.print(renderable)
    return stream.getvalue()


def test_welcome_uses_a_compact_terminal_wordmark() -> None:
    status = RuntimeStatus(
        session_id="session-id",
        cwd="/workspace",
        provider_id="provider",
        base_url=None,
        model="model",
        permission_mode="default",
        credential_source="stored",
        context_entry_count=0,
        conversation_entry_count=0,
        todos=(),
    )

    rendered = _plain(welcome(status), width=80)

    assert "›_  my-code v" in rendered
    assert "█" not in rendered
    assert "__  __" not in rendered

    welcome_panel = welcome(status)
    assert isinstance(welcome_panel, Panel)
    assert isinstance(welcome_panel.renderable, Group)
    wordmark = welcome_panel.renderable.renderables[0]
    assert isinstance(wordmark, Text)
    assert wordmark.plain.startswith("›_  my-code v")
    assert wordmark.spans[0].style == "bold cyan"
    assert wordmark.spans[1].style == "bold italic"


def test_status_card_uses_aligned_fields_and_full_width_rounded_border() -> None:
    status = RuntimeStatus(
        session_id="session-id",
        cwd="/workspace/a-project-with-a-long-name",
        provider_id="openai",
        base_url="http://127.0.0.1:8317/v1",
        model="gpt-test",
        permission_mode="default",
        credential_source="stored",
        context_entry_count=7,
        conversation_entry_count=9,
        todos=(),
        tool_count=4,
        skill_count=2,
        mcp_connected_count=1,
        mcp_server_count=2,
    )
    context = ContextStatus(
        reported_base_tokens=None,
        estimated_delta_tokens=50_000,
        projected_tokens=50_000,
        reserved_output_tokens=0,
        context_entry_count=7,
        conversation_entry_count=9,
        replacement_count=0,
        compact_count=0,
        input_limit_tokens=200_000,
    )

    rendered = _plain(render_status_card(status, context), width=72)

    assert rendered.splitlines()[0].startswith("╭")
    assert rendered.splitlines()[0].endswith("╮")
    assert rendered.splitlines()[-1].startswith("╰")
    assert ">_ my-code v" in rendered
    assert "Model:" in rendered and "gpt-test" in rendered
    assert "Provider:" in rendered and "127.0.0.1:8317/v1" in rendered
    assert "75% left" in rendered


def test_work_group_adds_one_separator_only_before_a_final_answer() -> None:
    blocks = TurnBlockCoordinator()
    blocks.add_reasoning(ReasoningPresentation("summary", ("Checked the code.",)))
    blocks.add_text("I am checking another file.")
    tool_step = blocks.complete_step(has_tools=True)
    blocks.mark_work()
    blocks.add_text("Final answer.")
    final_step = blocks.complete_step(has_tools=False)

    rendered = _plain(Group(*tool_step, *final_step), width=50)
    divider_lines = [line for line in rendered.splitlines() if set(line) == {"─"}]

    assert len(divider_lines) == 1
    assert rendered.index("Checked the code") < rendered.index(divider_lines[0])
    assert rendered.index(divider_lines[0]) < rendered.index("Final answer")

    pure = TurnBlockCoordinator()
    pure.add_text("Just an answer.")
    pure_rendered = _plain(Group(*pure.complete_step(has_tools=False)), width=50)
    assert not any(set(line) == {"─"} for line in pure_rendered.splitlines())


def test_single_injected_context_item_has_no_internal_separator() -> None:
    rendered = _plain(
        injected_context_message(
            4,
            (HistoryContextItem("AGENTS.md", None, "workspace rules"),),
        ),
        width=48,
    )
    divider_lines = [line for line in rendered.splitlines() if set(line) == {"─"}]

    assert len(divider_lines) == 1
    assert len(divider_lines[0]) == 48
    assert "Injected context · request #4" in rendered
    assert rendered.index("AGENTS.md") < rendered.index("workspace rules")


def test_injected_context_items_use_shared_separator_between_origins() -> None:
    rendered = _plain(
        injected_context_message(
            5,
            (
                HistoryContextItem("AGENTS.md", None, "workspace rules"),
                HistoryContextItem("attachment", "tool_search_listing", "- TodoWrite"),
            ),
        ),
        width=48,
    )
    divider_lines = [line for line in rendered.splitlines() if set(line) == {"─"}]

    assert len(divider_lines) == 2
    assert all(len(line) == 48 for line in divider_lines)
    internal_separator = divider_lines[0]
    assert rendered.index("workspace rules") < rendered.index(internal_separator)
    assert rendered.index(internal_separator) < rendered.index(
        "attachment · tool_search_listing"
    )
    assert rendered.index("attachment · tool_search_listing") < rendered.index(
        "- TodoWrite"
    )


def test_codex_markdown_snapshot_and_streaming_renderer_match() -> None:
    markdown = """# One
## Two
### Three
#### Four
##### Five
###### Six

Paragraph with `code` and [link](https://example.test).

> quote

- first
  1. nested

```python
print("hello")
```

| Name | Value |
| --- | --- |
| alpha | 1 |

---
"""

    rendered = _plain(assistant_message(markdown), width=60)

    non_empty = [line for line in rendered.splitlines() if line]
    assert non_empty[:6] == [
        "# One",
        "## Two",
        "### Three",
        "#### Four",
        "##### Five",
        "###### Six",
    ]
    assert "> quote" in rendered
    assert 'print("hello")' in rendered
    assert "———" in rendered
    assert "────" in rendered
    streamed = fragment_list_to_text(
        to_formatted_text(streaming_assistant_message(markdown, 60))
    )
    final = fragment_list_to_text(
        to_formatted_text(streaming_renderable(assistant_message(markdown), 60))
    )
    assert re.sub(r"id=\d+", "id", streamed) == re.sub(r"id=\d+", "id", final)


def test_numbered_chinese_h2_keeps_visible_heading_marker_and_text() -> None:
    markdown = "## 4. 几个值得注意的点\n\n正文"

    rendered = _plain(assistant_message(markdown), width=60)
    streamed = fragment_list_to_text(
        to_formatted_text(streaming_assistant_message(markdown, 60))
    )

    assert rendered.splitlines()[0] == "## 4. 几个值得注意的点"
    assert streamed.splitlines()[0] == "## 4. 几个值得注意的点"


def test_tool_activity_keeps_launch_order_and_consecutive_category_runs() -> None:
    group = ToolActivityGroup()
    group.start("read", ToolUsePresentation("Read", "a.py", "Reading", "explore"))
    group.start("bash", ToolUsePresentation("Bash", "pytest", "Running", "command"))
    group.start("grep", ToolUsePresentation("Grep", "needle", "Searching", "explore"))
    group.finish("grep", ToolResultPresentation("3 matches"), is_error=False)
    group.finish("read", ToolResultPresentation("10 lines"), is_error=False)
    group.finish("bash", ToolResultPresentation("failed", "exit 1"), is_error=True)

    rendered = _plain(tool_activity_message(group))

    assert rendered.index("Read") < rendered.index("Bash") < rendered.index("Grep")
    assert rendered.count("• Explored") == 2
    assert "• Ran commands" in rendered
    assert "× Bash" in rendered


def test_file_diff_expands_only_in_final_tool_activity_and_sanitizes_text() -> None:
    diff = FileDiffPresentation(
        "src/example.py",
        "updated",
        1,
        1,
        (
            FileDiffHunk(
                9,
                1,
                9,
                1,
                (
                    FileDiffLine("deletion", "value\t= 1\x1b", old_line=9),
                    FileDiffLine("addition", "value\t= 2界", new_line=9),
                ),
            ),
        ),
    )
    result = ToolResultPresentation("updated", file_diff=diff)
    group = ToolActivityGroup()
    group.start("edit", ToolUsePresentation("Edit", "example.py", "Editing", "change"))
    group.finish("edit", result, is_error=False)

    compact = _plain(tool_activity_message(group, expand_diffs=False), width=32)
    expanded = _plain(tool_activity_message(group), width=32)
    direct = _plain(file_diff_message(diff), width=32)

    assert "value" not in compact
    assert "Edited src/example.py (+1" in expanded
    assert "-1)" in expanded
    assert "value   = 2界" in expanded
    assert "\\x1b" in expanded
    assert direct == _plain(file_diff_message(diff), width=32)


def test_diff_rendering_wraps_long_lines_without_repeating_line_number() -> None:
    diff = FileDiffPresentation(
        "x.txt",
        "created",
        1,
        0,
        (
            FileDiffHunk(
                0,
                0,
                12,
                1,
                (FileDiffLine("addition", "a very long line that wraps", new_line=12),),
            ),
        ),
    )

    rendered = _plain(file_diff_message(diff), width=20)

    assert rendered.count("12 +") == 1
    assert "Created x.txt (+1" in rendered


def test_diff_rendering_uses_line_and_word_backgrounds() -> None:
    diff = FileDiffPresentation(
        "x.py",
        "updated",
        1,
        1,
        (
            FileDiffHunk(
                1,
                1,
                1,
                1,
                (
                    FileDiffLine("deletion", "value = 1", old_line=1),
                    FileDiffLine("addition", "value = 2", new_line=1),
                ),
            ),
        ),
    )

    rendered = _ansi(file_diff_message(diff))

    assert "48;2;74;31;36" in rendered
    assert "48;2;118;45;56" in rendered
    assert "48;2;18;61;39" in rendered
    assert "48;2;31;99;61" in rendered


def test_todo_snapshot_is_ephemeral_complete_and_supports_empty_state() -> None:
    rendered = _plain(
        todo_snapshot(
            (
                TodoItem("done", "completed", "done"),
                TodoItem("now", "in_progress", "doing"),
                TodoItem("later", "pending", "waiting"),
            )
        )
    )

    assert rendered.splitlines() == [
        "• Updated Plan",
        "  ✔ done",
        "  □ now",
        "  □ later",
    ]
    assert "(no tasks)" in _plain(todo_snapshot(()))


class _TranscriptSource:
    def __init__(self, view: TranscriptView) -> None:
        self.view = view

    def current_transcript_view(self) -> TranscriptView:
        return self.view


def test_transcript_renders_structured_values_and_literal_tool_output() -> None:
    view = TranscriptView(
        1,
        (
            TranscriptText("user", "hello"),
            TranscriptToolResult("Read", "**not markdown**", False),
        ),
    )
    rendered = _plain(transcript_renderable(view))

    assert "› User" in rendered
    assert "**not markdown**" in rendered

    value = TranscriptValue(
        "object",
        fields=(
            TranscriptField(
                "items",
                TranscriptValue(
                    "array", items=(TranscriptValue("scalar", scalar="one"),)
                ),
            ),
        ),
    )
    source = _TranscriptSource(TranscriptView(2, ()))
    source.view = TranscriptView(3, ())
    assert value.fields[0].value.items[0].scalar == "one"


def test_transcript_separates_visible_work_from_final_answer() -> None:
    view = TranscriptView(
        1,
        (
            TranscriptReasoning(
                ReasoningPresentation("summary", ("Inspected the implementation.",))
            ),
            TranscriptText("assistant", "Final answer.", is_final_answer=True),
        ),
    )

    rendered = _plain(transcript_renderable(view), width=48)

    assert len([line for line in rendered.splitlines() if set(line) == {"─"}]) == 1


def test_transcript_pager_starts_at_tail_and_preserves_position_on_refresh() -> None:
    source = _TranscriptSource(
        TranscriptView(
            1,
            tuple(TranscriptText("assistant", f"line {index}") for index in range(80)),
        )
    )
    pager = TranscriptPager(source, output=DummyOutput())
    assert pager.application.ttimeoutlen == 0.05
    assert pager.application.timeoutlen == 0.0
    pager._refresh()

    assert pager._follow_tail
    assert pager._top == pager._max_top()

    pager.scroll(-5)
    old_top = pager._top
    source.view = TranscriptView(
        2,
        source.view.entries + (TranscriptText("assistant", "new persisted entry"),),
    )
    pager._refresh()

    assert not pager._follow_tail
    assert pager._top == old_top

    pager._follow_tail = True
    source.view = TranscriptView(
        3, source.view.entries + (TranscriptText("assistant", "another entry"),)
    )
    pager._refresh()
    assert pager._top == pager._max_top()
