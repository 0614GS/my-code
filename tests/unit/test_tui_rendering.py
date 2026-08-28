"""Snapshots for Codex-style scrollback and transcript presentation."""

import re
from io import StringIO

from prompt_toolkit.formatted_text import fragment_list_to_text, to_formatted_text
from prompt_toolkit.output import DummyOutput
from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text

from my_code.chat.status import RuntimeStatus
from my_code.chat.views import (
    TranscriptField,
    TranscriptText,
    TranscriptToolResult,
    TranscriptValue,
    TranscriptView,
)
from my_code.conversation.presentation import ToolResultPresentation
from my_code.features.todos.models import TodoItem
from my_code.tools.presentation import ToolUsePresentation
from my_code.tui.activity import ToolActivityGroup
from my_code.tui.transcript import TranscriptPager, transcript_renderable
from my_code.tui.widgets import (
    assistant_message,
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
