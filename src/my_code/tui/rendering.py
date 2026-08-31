"""Bounded live projection and latest-wins render coordination."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from io import StringIO
from typing import Any, cast

from prompt_toolkit.application import in_terminal
from prompt_toolkit.formatted_text import ANSI, FormattedText, to_formatted_text
from rich.console import Console, RenderableType
from rich.padding import Padding

from my_code.tui.widgets import assistant_message

_MAX_UNSTABLE_MARKDOWN = 16 * 1024
_FALLBACK_TEXT_CHARS = 8 * 1024
_MAX_VISUAL_LINES = 12
_PROTECTED_VISUAL_LINES = 3


@dataclass(frozen=True, slots=True)
class StreamingProjection:
    """一次流式投影产生的稳定片段和仍需原地显示的尾部。"""

    committed: tuple[str, ...]
    tail: FormattedText


class StreamingMarkdownProjector:
    """把稳定视觉行提交给 scrollback，只在动态区保留易变尾部。"""

    def __init__(self) -> None:
        self._width = 0
        self._source = ""
        self._source_offset = 0
        self._rendered_lines: tuple[str, ...] = ()
        self._committed_lines = 0
        self.last_rich_input_chars = 0

    def invalidate(self) -> None:
        self._width = 0
        self._source = ""
        self._source_offset = 0
        self._rendered_lines = ()
        self._committed_lines = 0

    def update(self, source: str, width: int) -> StreamingProjection:
        """推进 append-only Markdown，并返回新稳定行与受保护尾部。"""

        width = max(width, 20)
        committed: list[str] = []
        width_changed = bool(self._width and width != self._width)
        replaced = bool(self._width and not source.startswith(self._source))
        crossed_render_bound = (
            len(source) - self._source_offset > _MAX_UNSTABLE_MARKDOWN
            and len(self._source) - self._source_offset <= _MAX_UNSTABLE_MARKDOWN
        )
        if width_changed or replaced or crossed_render_bound:
            # 已经显示的视觉行无法在 terminal scrollback 中重排。宽度变化或
            # provider 替换快照时，先按旧宽度固化动态尾部，再从新快照继续。
            committed.extend(self._uncommitted_lines())
            self._rendered_lines = ()
            self._committed_lines = 0
            self._source_offset = (
                len(self._source) if width_changed or crossed_render_bound else 0
            )

        rendered = self._render_bounded(source[self._source_offset :], width)
        lines = tuple(rendered.splitlines())
        previous = self._uncommitted_lines()
        current = lines[self._committed_lines :]
        common = _common_prefix_length(previous, current)
        stable_count = max(0, common - _PROTECTED_VISUAL_LINES)
        if stable_count:
            committed.extend(current[:stable_count])
            self._committed_lines += stable_count

        self._width = width
        self._source = source
        self._rendered_lines = lines
        tail_lines = lines[self._committed_lines :]
        tail = "\n".join(tail_lines[-_MAX_VISUAL_LINES:])
        return StreamingProjection(
            (_lines_fragment(tuple(committed)),) if committed else (),
            FormattedText(to_formatted_text(ANSI(tail))),
        )

    def flush(self, source: str, width: int) -> StreamingProjection:
        """固化最终快照中尚未提交的全部视觉行，并清空动态尾部。"""

        projection = self.update(source, width)
        fragments = list(projection.committed)
        remaining = self._uncommitted_lines()
        if remaining:
            fragments.append(_lines_fragment(remaining))
        self.invalidate()
        return StreamingProjection(tuple(fragments), FormattedText())

    def project(self, source: str, width: int) -> FormattedText:
        """兼容只读预览调用；状态推进由 ``update`` 显式完成。"""

        if source == self._source and max(width, 20) == self._width:
            tail = "\n".join(self._uncommitted_lines()[-_MAX_VISUAL_LINES:])
            return FormattedText(to_formatted_text(ANSI(tail)))
        rendered = self._render_bounded(source, max(width, 20))
        tail = _tail_lines(rendered, _MAX_VISUAL_LINES)
        return FormattedText(to_formatted_text(ANSI(tail)))

    def _render_bounded(self, source: str, width: int) -> str:
        if len(source) > _MAX_UNSTABLE_MARKDOWN:
            rendered = source[-_FALLBACK_TEXT_CHARS:]
            self.last_rich_input_chars = 0
        else:
            rendered = self._render_markdown(source, width)
        return rendered

    def _uncommitted_lines(self) -> tuple[str, ...]:
        return self._rendered_lines[self._committed_lines :]

    def _render_markdown(self, source: str, width: int) -> str:
        if not source:
            self.last_rich_input_chars = 0
            return ""
        self.last_rich_input_chars = len(source)
        stream = StringIO()
        console = Console(
            file=stream,
            width=width,
            force_terminal=True,
            color_system="truecolor",
        )
        console.print(assistant_message(source), end="")
        return "\n".join(line.rstrip() for line in stream.getvalue().splitlines())


@dataclass(frozen=True, slots=True)
class _RenderRequest:
    revision: int
    source: str
    width: int
    callback: Callable[[int, FormattedText], None]


class RenderCoordinator:
    """One in-flight worker render with debouncing and revision rejection."""

    def __init__(
        self,
        projector: StreamingMarkdownProjector,
        *,
        frame_interval: float = 0.04,
    ) -> None:
        self._projector = projector
        self._frame_interval = frame_interval
        self._revision = 0
        self._latest: _RenderRequest | None = None
        self._timer: asyncio.TimerHandle | None = None
        self._worker: asyncio.Task[None] | None = None
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="my-code-live-render"
        )
        self._closed = False

    def request(
        self,
        source: str,
        width: int,
        callback: Callable[[int, FormattedText], None],
        *,
        structural: bool = False,
    ) -> int:
        self._revision += 1
        request = _RenderRequest(self._revision, source, width, callback)
        self._latest = request
        if self._closed:
            return request.revision
        if structural and self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._worker is not None:
            return request.revision
        if structural:
            self._start_latest()
        elif self._timer is None:
            self._timer = asyncio.get_running_loop().call_later(
                self._frame_interval, self._start_latest
            )
        return request.revision

    def _start_latest(self) -> None:
        self._timer = None
        if self._closed or self._latest is None or self._worker is not None:
            return
        request = self._latest
        self._worker = asyncio.create_task(self._render(request))

    def clear(self) -> None:
        """Invalidate pending/in-flight results and cancel a deferred frame."""

        self._revision += 1
        self._latest = None
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    async def _render(self, request: _RenderRequest) -> None:
        try:
            frames: list[FormattedText] = []
            errors: list[BaseException] = []

            def project() -> None:
                try:
                    frames.append(
                        self._projector.project(request.source, request.width)
                    )
                except BaseException as error:
                    errors.append(error)

            await asyncio.get_running_loop().run_in_executor(self._executor, project)
            if errors:
                raise errors[0]
            frame = frames[0]
            if request.revision == self._revision and not self._closed:
                request.callback(request.revision, frame)
        finally:
            self._worker = None
            if (
                not self._closed
                and self._latest is not None
                and self._latest.revision > request.revision
            ):
                self._start_latest()

    async def close(self) -> None:
        self._closed = True
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        worker = self._worker
        if worker is not None:
            await asyncio.gather(worker, return_exceptions=True)
        self._executor.shutdown(wait=True, cancel_futures=True)


class ScrollbackWriter:
    """Serialize off-screen Rich rendering and short terminal commits."""

    def __init__(self, console: Console) -> None:
        self._console = console
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="my-code-render"
        )
        self._lock = asyncio.Lock()
        self._has_output = False
        self._last_was_user = False
        self._closed = False

    def seed(self, has_output: bool, last_was_user: bool) -> None:
        """Adopt output written synchronously before the application starts."""

        self._has_output = has_output
        self._last_was_user = last_was_user

    async def write(self, renderable: RenderableType, *, clear: bool = False) -> None:
        await self.write_many((renderable,), clear=clear)

    async def write_many(
        self, renderables: tuple[RenderableType, ...], *, clear: bool = False
    ) -> None:
        if not renderables:
            return
        async with self._lock:
            if self._closed:
                return
            loop = asyncio.get_running_loop()
            snapshot = await loop.run_in_executor(
                self._executor,
                self._render_many,
                renderables,
                clear,
                self._has_output,
                self._last_was_user,
            )
            async with in_terminal():
                if clear:
                    self._console.clear()
                self._console.file.write(snapshot)
                self._console.file.flush()
            self._has_output = True
            self._last_was_user = isinstance(renderables[-1], Padding)

    async def write_stream_fragment(self, fragment: str, *, first: bool) -> None:
        """写入已渲染视觉行；同一回答的片段之间不增加卡片间距。"""

        if not fragment:
            return
        async with self._lock:
            if self._closed:
                return
            prefix = (
                "\n" if first and self._has_output and not self._last_was_user else ""
            )
            async with in_terminal():
                self._console.file.write(prefix + fragment)
                self._console.file.flush()
            self._has_output = True
            self._last_was_user = False

    def _render_many(
        self,
        renderables: tuple[RenderableType, ...],
        clear: bool,
        has_output: bool,
        last_was_user: bool,
    ) -> str:
        stream = StringIO()
        console = Console(
            file=stream,
            width=self._console.width,
            force_terminal=self._console.is_terminal,
            color_system=cast(Any, self._console.color_system),
        )
        current_has_output = False if clear else has_output
        current_last_user = False if clear else last_was_user
        for renderable in renderables:
            is_user = isinstance(renderable, Padding)
            if current_has_output and not current_last_user and not is_user:
                console.print()
            console.print(renderable)
            current_has_output = True
            current_last_user = is_user
        return stream.getvalue()

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=True)


def _tail_lines(value: str, count: int) -> str:
    return "\n".join(value.splitlines()[-count:])


def _common_prefix_length(left: tuple[str, ...], right: tuple[str, ...]) -> int:
    count = 0
    for old, new in zip(left, right, strict=False):
        if old != new:
            break
        count += 1
    return count


def _lines_fragment(lines: tuple[str, ...]) -> str:
    return "\n".join(lines) + "\n"


__all__ = [
    "RenderCoordinator",
    "ScrollbackWriter",
    "StreamingProjection",
    "StreamingMarkdownProjector",
]
