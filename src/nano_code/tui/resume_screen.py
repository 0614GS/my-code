"""当前项目会话的轻量恢复选择器。"""

from datetime import UTC, datetime

from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, OptionList
from textual.widgets.option_list import Option

from nano_code.sessions import SessionSummary


class ResumeScreen(ModalScreen[str | None]):
    """只负责展示和选择会话，不直接读取 JSONL。"""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(
        self,
        sessions: tuple[SessionSummary, ...],
        *,
        now: datetime | None = None,
    ) -> None:
        super().__init__()
        self.sessions = sessions
        self.now = now or datetime.now(UTC)

    def compose(self) -> ComposeResult:
        options = [
            Option(_render_session(summary, self.now), id=summary.session_id)
            for summary in self.sessions
        ]
        with Vertical(id="resume-dialog"):
            yield Label("Resume a conversation", id="resume-title")
            yield Label(
                "Select a session from this project",
                id="resume-description",
            )
            yield OptionList(*options, id="resume-list", compact=True)
            yield Label(
                "↑↓ select · Enter resume · Esc cancel",
                id="resume-hint",
            )

    def on_mount(self) -> None:
        session_list = self.query_one("#resume-list", OptionList)
        if session_list.option_count:
            session_list.highlighted = 0
        session_list.focus()

    @on(OptionList.OptionSelected, "#resume-list")
    def select_session(self, event: OptionList.OptionSelected) -> None:
        if event.option_id is not None:
            self.dismiss(str(event.option_id))

    def action_cancel(self) -> None:
        self.dismiss(None)


def _render_session(summary: SessionSummary, now: datetime) -> Table:
    """首行展示标题，次行在最右侧展示相对时间。"""

    table = Table.grid(expand=True, padding=0)
    table.add_column(ratio=1, no_wrap=True)
    table.add_column(justify="right", no_wrap=True)
    table.add_row(Text(summary.title, style="bold #e8e4df"), "")
    table.add_row(
        "",
        Text(_relative_time(summary.updated_at, now), style="dim"),
    )
    return table


def _relative_time(value: datetime, now: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    seconds = max(0, int((now.astimezone(UTC) - value.astimezone(UTC)).total_seconds()))
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 30:
        return f"{days}d ago"
    months = days // 30
    if months < 12:
        return f"{months}mo ago"
    return f"{days // 365}y ago"
