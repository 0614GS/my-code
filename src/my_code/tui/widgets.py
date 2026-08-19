"""my-code 终端设计系统中可复用的 Textual 组件。"""

from __future__ import annotations

import asyncio

from rich.console import Group, RenderableType
from rich.markdown import Markdown as RichMarkdown
from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.events import Click, Key
from textual.widgets import Input, Label, LoadingIndicator, OptionList, Static
from textual.widgets.option_list import Option

from my_code import __version__
from my_code.chat.permissions import PermissionRequest
from my_code.chat.status import RuntimeStatus
from my_code.features.todos.models import TodoItem
from my_code.model.primitives import ReasoningDisclosure, ReasoningPresentation
from my_code.permissions.models import (
    PermissionBehavior,
    PermissionConfirmation,
    PermissionUpdate,
    PermissionUpdateDestination,
)
from my_code.permissions.rules import validate_bash_rule_content
from my_code.permissions.updates import permission_rule_for_destination
from my_code.tools.presentation import ToolResultPresentation, ToolUsePresentation


class WelcomePanel(Static):
    def __init__(self, status: RuntimeStatus) -> None:
        super().__init__()
        self.status = status

    def render(self) -> RenderableType:
        title = Text.assemble(
            ("✦ my-code", "bold #ff9b73"),
            (f"  v{__version__}", "dim"),
        )
        subtitle = Text("A small coding agent with explicit boundaries", style="bold")
        model = Text.assemble(
            (self.status.provider_id, "#ffb38a"),
            (" / ", "dim"),
            (self.status.model, "#d9d3cd"),
            ("  ·  ", "dim"),
            (self.status.credential_source, "dim"),
        )
        workspace = Text(self.status.cwd, style="dim")
        return Group(title, Text(), subtitle, model, workspace)


class UserMessage(Static):
    def __init__(self, prompt: str) -> None:
        super().__init__(classes="message")
        self.prompt = prompt

    def render(self) -> RenderableType:
        return Text.assemble(("❯ ", "bold #ff9b73"), self.prompt)


class AssistantMessage(Static):
    """按有界刷新批次渲染的增量 Markdown。"""

    def __init__(self, content: str) -> None:
        super().__init__(RichMarkdown(content), classes="message")
        self._content = content
        self._pending = ""
        self._flush_task: asyncio.Task[None] | None = None

    @property
    def source(self) -> str:
        return self._content + self._pending

    async def append_delta(self, content: str) -> None:
        self._pending += content
        if self._flush_task is None:
            self._flush_task = asyncio.create_task(self._flush_after_delay())
        await asyncio.sleep(0)

    async def finish_stream(self) -> None:
        if self._flush_task is not None:
            await self._flush_task
        self._apply_pending()

    async def complete_stream(self, content: str) -> None:
        """Replace accumulated deltas with the provider's completed snapshot."""

        if self._flush_task is not None:
            await self._flush_task
        self._pending = ""
        self._content = content
        self.update(RichMarkdown(content))

    async def _flush_after_delay(self) -> None:
        try:
            # 合并高频 token 片段，使 Markdown 解析保持响应且工作量有界，
            # 同时不遗留长期运行的 worker。
            await asyncio.sleep(1 / 30)
            self._apply_pending()
        finally:
            self._flush_task = None

    def _apply_pending(self) -> None:
        if not self._pending:
            return
        self._content += self._pending
        self._pending = ""
        self.update(RichMarkdown(self._content))


class ReasoningMessage(Static):
    """只消费安全 presentation 的可折叠 reasoning 展示。"""

    def __init__(
        self,
        disclosure: ReasoningDisclosure,
        *,
        expanded: bool = True,
    ) -> None:
        super().__init__(classes="message reasoning")
        self.disclosure = disclosure
        self.parts: list[str] = []
        self.expanded = expanded
        self.completed = False
        self.interrupted = False

    def append_delta(self, part_index: int, content: str) -> None:
        while len(self.parts) <= part_index:
            self.parts.append("")
        self.parts[part_index] += content
        self.refresh(layout=True)

    def finish(self) -> None:
        self.completed = True
        self.refresh(layout=True)

    def interrupt(self) -> None:
        self.completed = True
        self.interrupted = True
        self.refresh(layout=True)

    def load_presentation(self, presentation: ReasoningPresentation) -> None:
        self.disclosure = presentation.disclosure
        self.parts = list(presentation.parts)
        self.completed = True
        self.refresh(layout=True)

    def on_click(self, _: Click) -> None:
        if self.completed:
            self.expanded = not self.expanded
            self.refresh(layout=True)

    def render(self) -> RenderableType:
        title = "思考过程" if self.disclosure == "verbatim" else "思考摘要"
        if self.disclosure == "redacted":
            body = "思考内容已由模型提供方隐藏"
        elif self.disclosure == "hidden":
            body = "模型已完成思考"
        else:
            body = "\n\n".join(self.parts)
        suffix = " · 已中断" if self.interrupted else ""
        marker = "▾" if self.expanded else "▸"
        header = Text(f"{marker} {title}{suffix}", style="dim italic")
        if not self.expanded:
            return header
        return Group(header, RichMarkdown(body) if body else Text("…", style="dim"))


class ToolCallMessage(Static):
    """工具执行完成时原地更新的一行稳定展示。"""

    def __init__(self, tool_use_id: str, presentation: ToolUsePresentation) -> None:
        super().__init__(classes="message tool-call")
        self.tool_use_id = tool_use_id
        self.presentation = presentation
        self.result: ToolResultPresentation | None = None
        self.is_error = False

    def finish(self, presentation: ToolResultPresentation, *, is_error: bool) -> None:
        self.result = presentation
        self.is_error = is_error
        self.set_class(is_error, "error")
        self.refresh()

    def render(self) -> RenderableType:
        marker = "×" if self.is_error else ("●" if self.result is None else "✓")
        marker_style = "#ff7b72" if self.is_error else "#d97757"
        line = Text.assemble(
            (f"{marker} ", f"bold {marker_style}"),
            (self.presentation.display_name, "bold #d9d3cd"),
            (f"({self.presentation.summary})", "#a9a19a"),
        )
        if self.result is not None:
            line.append("\n  ⎿ ", style="dim")
            line.append(
                self.result.summary,
                style="#8f8882" if not self.is_error else "#ff7b72",
            )
            if self.result.detail:
                line.append(f"\n     {self.result.detail}", style="dim")
        return line


class SystemMessage(Static):
    def __init__(self, content: str, *, error: bool = False) -> None:
        classes = "message error" if error else "message"
        super().__init__(content, classes=classes, markup=False)


class TodoPanel(Static):
    """当前 session TodoList 的可折叠、前端本地展示。"""

    def __init__(self, todos: tuple[TodoItem, ...]) -> None:
        super().__init__()
        self.todos = todos
        self.expanded = bool(todos)
        self.display = bool(todos)

    def set_todos(
        self,
        todos: tuple[TodoItem, ...],
        *,
        reset_session: bool = False,
    ) -> None:
        was_empty = not self.todos
        self.todos = todos
        if reset_session or (was_empty and todos):
            self.expanded = bool(todos)
        elif not todos:
            self.expanded = False
        self.display = bool(todos)
        self.refresh(layout=True)

    def toggle(self) -> None:
        if not self.todos:
            return
        self.expanded = not self.expanded
        self.refresh(layout=True)

    def render(self) -> RenderableType:
        pending = sum(todo.status == "pending" for todo in self.todos)
        in_progress = sum(todo.status == "in_progress" for todo in self.todos)
        completed = sum(todo.status == "completed" for todo in self.todos)
        header = Text.assemble(
            ("Tasks", "bold #d9d3cd"),
            (
                f"  {in_progress} in progress · {pending} pending · {completed} done",
                "#8f8882",
            ),
            ("  Ctrl+T", "dim"),
        )
        if not self.expanded:
            active = next(
                (todo.content for todo in self.todos if todo.status == "in_progress"),
                None,
            )
            if active is not None:
                header.append(f"\n  ● {active}", style="#d97757")
            return header

        lines: list[Text] = [header]
        for todo in self.todos:
            if todo.status == "completed":
                lines.append(Text(f"  ✓ {todo.content}", style="dim strike"))
            elif todo.status == "in_progress":
                lines.append(Text(f"  ● {todo.content}", style="#ffb38a"))
            else:
                lines.append(Text(f"  ○ {todo.content}", style="#a9a19a"))
        return Group(*lines)


class ActivityBar(Horizontal):
    def __init__(self, todos: tuple[TodoItem, ...] = ()) -> None:
        super().__init__()
        self.todos = todos

    def compose(self) -> ComposeResult:
        yield LoadingIndicator()
        yield Label(self._activity_text())

    def set_todos(self, todos: tuple[TodoItem, ...]) -> None:
        self.todos = todos
        self.query_one(Label).update(self._activity_text())

    def _activity_text(self) -> str:
        active = next(
            (todo for todo in self.todos if todo.status == "in_progress"), None
        )
        return f"{active.active_form}…" if active is not None else "my-code is working…"


class StatusBar(Static):
    def __init__(self, status: RuntimeStatus, context_usage: str) -> None:
        super().__init__()
        self.set_status(status, context_usage)

    def set_status(self, status: RuntimeStatus, context_usage: str) -> None:
        self.context_usage = context_usage
        left = Text.assemble(
            (status.model, "dim"),
            ("  ·  ", "dim"),
            (status.permission_mode, "dim"),
            ("  ·  ", "dim"),
            (f"{status.working_message_count} messages", "dim"),
            (
                "    Ctrl+T todos" if status.todos else "    / for commands",
                "#a59c94",
            ),
        )
        table = Table.grid(expand=True, padding=(0, 0))
        table.add_column()
        table.add_column(justify="right", no_wrap=True)
        table.add_row(left, Text(context_usage, style="dim"))
        self.update(table)


class PermissionPanel(Vertical):
    """临时替换输入框的 Claude 风格行内选择器。"""

    BINDINGS = [
        Binding("escape", "deny", "Deny", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._future: asyncio.Future[PermissionConfirmation] | None = None
        self._request: PermissionRequest | None = None

    def compose(self) -> ComposeResult:
        yield Label("Tool use", id="permission-title")
        yield Static(id="permission-detail")
        yield Label("Do you want to proceed?", id="permission-question")
        yield OptionList(
            Option("1. Yes", id="yes"),
            Option("2. No", id="no"),
            Option("3. No, and tell my-code why", id="feedback"),
            Option("4. Yes, and don't ask again", id="remember"),
            id="permission-options",
            compact=True,
        )
        yield Input(
            placeholder="Tell my-code what to do differently",
            id="permission-feedback",
            max_length=1000,
        )
        yield Input(
            placeholder="Command prefix to allow (e.g., git diff:*)",
            id="permission-prefix",
            max_length=1000,
        )
        yield Label("↑↓ select · Enter confirm · Esc deny", id="permission-hint")

    async def ask(self, request: PermissionRequest) -> PermissionConfirmation:
        if self._future is not None:
            raise RuntimeError("A permission request is already active")
        detail = Text.assemble(
            (request.presentation.display_name, "bold #d9d3cd"),
            (f"({request.presentation.summary})", "#a9a19a"),
            (f"\n{request.message}", "#8f8882"),
        )
        self.query_one("#permission-detail", Static).update(detail)
        options = self.query_one("#permission-options", OptionList)
        feedback = self.query_one("#permission-feedback", Input)
        prefix = self.query_one("#permission-prefix", Input)
        feedback.value = ""
        feedback.display = False
        prefix.value = ""
        prefix.display = False
        choices = [
            Option("1. Yes", id="yes"),
            Option("2. No", id="no"),
            Option("3. No, and tell my-code why", id="feedback"),
        ]
        if request.tool_name == "Bash" or request.suggestions:
            choices.append(Option("4. Yes, and don't ask again", id="remember"))
        options.set_options(choices)
        options.display = True
        options.highlighted = 0
        self.display = True
        options.focus()
        self._request = request
        self._future = asyncio.get_running_loop().create_future()
        try:
            return await self._future
        finally:
            self._future = None
            self._request = None
            self.display = False

    @on(OptionList.OptionSelected, "#permission-options")
    def select_permission(self, event: OptionList.OptionSelected) -> None:
        if event.option_id == "yes":
            self._resolve(PermissionConfirmation(True))
        elif event.option_id == "no":
            self._resolve(PermissionConfirmation(False))
        elif event.option_id == "feedback":
            self.action_feedback()
        elif event.option_id == "remember":
            self.action_remember()

    def on_key(self, event: Key) -> None:
        """数字快捷键只作用于选择器，不影响反馈文本输入。"""

        if not self.query_one("#permission-options", OptionList).has_focus:
            return
        actions = {
            "1": self.action_allow,
            "2": self.action_deny,
            "3": self.action_feedback,
            "4": self.action_remember,
        }
        action = actions.get(event.key)
        if action is not None:
            action()
            event.prevent_default()
            event.stop()

    @on(Input.Submitted, "#permission-feedback")
    def submit_feedback(self, event: Input.Submitted) -> None:
        feedback = event.value.strip()
        if not feedback:
            event.input.placeholder = (
                "Please explain what my-code should do differently"
            )
            return
        self._resolve(PermissionConfirmation(False, feedback))

    @on(Input.Submitted, "#permission-prefix")
    def submit_prefix(self, event: Input.Submitted) -> None:
        raw = event.value.strip()
        try:
            content = validate_bash_rule_content(raw)
        except ValueError as error:
            event.input.placeholder = f"Invalid prefix: {error}"
            event.input.value = ""
            return
        self._resolve(
            PermissionConfirmation(
                True,
                updates=(
                    PermissionUpdate.add_rules(
                        (
                            permission_rule_for_destination(
                                "Bash",
                                PermissionBehavior.ALLOW,
                                PermissionUpdateDestination.LOCAL,
                                content,
                            ),
                        ),
                        destination=PermissionUpdateDestination.LOCAL,
                    ),
                ),
            )
        )

    def action_allow(self) -> None:
        self._resolve(PermissionConfirmation(True))

    def action_deny(self) -> None:
        self._resolve(PermissionConfirmation(False))

    def action_feedback(self) -> None:
        self.query_one("#permission-options", OptionList).display = False
        feedback = self.query_one("#permission-feedback", Input)
        feedback.display = True
        feedback.focus()

    def action_remember(self) -> None:
        request = self._request
        if request is None:
            return
        if request.tool_name == "Bash":
            self.query_one("#permission-options", OptionList).display = False
            prefix = self.query_one("#permission-prefix", Input)
            prefix.display = True
            prefix.focus()
            return
        if request.suggestions:
            self._resolve(PermissionConfirmation(True, updates=request.suggestions))

    def _resolve(self, response: PermissionConfirmation) -> None:
        if self._future is not None and not self._future.done():
            self._future.set_result(response)
