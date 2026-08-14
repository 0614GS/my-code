"""nano-code 终端设计系统中可复用的 Textual 组件。"""

from __future__ import annotations

import asyncio

from rich.console import Group, RenderableType
from rich.markdown import Markdown as RichMarkdown
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.events import Key
from textual.widgets import Input, Label, LoadingIndicator, OptionList, Static
from textual.widgets.option_list import Option

from nano_code import __version__
from nano_code.permissions import PermissionConfirmation
from nano_code.presentation import ToolResultPresentation, ToolUsePresentation
from nano_code.tui.contracts import PermissionRequest, RuntimeStatus


class WelcomePanel(Static):
    def __init__(self, status: RuntimeStatus) -> None:
        super().__init__()
        self.status = status

    def render(self) -> RenderableType:
        title = Text.assemble(
            ("✦ nano-code", "bold #ff9b73"),
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


class ActivityBar(Horizontal):
    def compose(self) -> ComposeResult:
        yield LoadingIndicator()
        yield Label("nano-code is working…")


class StatusBar(Static):
    def __init__(self, status: RuntimeStatus) -> None:
        super().__init__()
        self.set_status(status)

    def set_status(self, status: RuntimeStatus) -> None:
        self.update(
            Text.assemble(
                (status.model, "dim"),
                ("  ·  ", "dim"),
                (status.permission_mode, "dim"),
                ("  ·  ", "dim"),
                (f"{status.message_count} messages", "dim"),
                ("    / for commands", "#a59c94"),
            )
        )


class PermissionPanel(Vertical):
    """临时替换输入框的 Claude 风格行内选择器。"""

    BINDINGS = [
        Binding("escape", "deny", "Deny", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._future: asyncio.Future[PermissionConfirmation] | None = None

    def compose(self) -> ComposeResult:
        yield Label("Tool use", id="permission-title")
        yield Static(id="permission-detail")
        yield Label("Do you want to proceed?", id="permission-question")
        yield OptionList(
            Option("1. Yes", id="yes"),
            Option("2. No", id="no"),
            Option("3. No, and tell nano-code why", id="feedback"),
            id="permission-options",
            compact=True,
        )
        yield Input(
            placeholder="Tell nano-code what to do differently",
            id="permission-feedback",
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
        feedback.value = ""
        feedback.display = False
        options.display = True
        options.highlighted = 0
        self.display = True
        options.focus()
        self._future = asyncio.get_running_loop().create_future()
        try:
            return await self._future
        finally:
            self._future = None
            self.display = False

    @on(OptionList.OptionSelected, "#permission-options")
    def select_permission(self, event: OptionList.OptionSelected) -> None:
        if event.option_id == "yes":
            self._resolve(PermissionConfirmation(True))
        elif event.option_id == "no":
            self._resolve(PermissionConfirmation(False))
        elif event.option_id == "feedback":
            self.action_feedback()

    def on_key(self, event: Key) -> None:
        """数字快捷键只作用于选择器，不影响反馈文本输入。"""

        if not self.query_one("#permission-options", OptionList).has_focus:
            return
        actions = {
            "1": self.action_allow,
            "2": self.action_deny,
            "3": self.action_feedback,
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
                "Please explain what nano-code should do differently"
            )
            return
        self._resolve(PermissionConfirmation(False, feedback))

    def action_allow(self) -> None:
        self._resolve(PermissionConfirmation(True))

    def action_deny(self) -> None:
        self._resolve(PermissionConfirmation(False))

    def action_feedback(self) -> None:
        self.query_one("#permission-options", OptionList).display = False
        feedback = self.query_one("#permission-feedback", Input)
        feedback.display = True
        feedback.focus()

    def _resolve(self, response: PermissionConfirmation) -> None:
        if self._future is not None and not self._future.done():
            self._future.set_result(response)
