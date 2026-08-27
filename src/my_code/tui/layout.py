"""prompt_toolkit layout construction for the inline terminal host."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from prompt_toolkit.application import Application
from prompt_toolkit.filters import Condition, has_completions, is_done
from prompt_toolkit.formatted_text import AnyFormattedText, to_formatted_text
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (
    BufferControl,
    ConditionalContainer,
    HSplit,
    Layout,
    ScrollOffsets,
    Window,
)
from prompt_toolkit.layout.containers import WindowAlign
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import CompletionsMenuControl
from prompt_toolkit.output import Output

from my_code.tui.dimensions import SURFACE_VERTICAL_PADDING
from my_code.tui.terminal import terminal_color_depth, terminal_output
from my_code.tui.theme import TuiTheme


@dataclass(frozen=True, slots=True)
class TerminalLayout:
    application: Application[None]
    body: HSplit
    completions_menu: ConditionalContainer
    slash_menu: ConditionalContainer


def build_terminal_layout(
    *,
    input_control: BufferControl,
    key_bindings: KeyBindings,
    dynamic_text: Callable[[], AnyFormattedText],
    todo_text: Callable[[], AnyFormattedText],
    has_todos: Callable[[], bool],
    status_text: Callable[[], str],
    slash_menu_text: Callable[[], AnyFormattedText],
    has_slash_menu: Callable[[], bool],
    input: Input | None,
    output: Output | None,
    theme: TuiTheme,
) -> TerminalLayout:
    """Build the non-full-screen dynamic area below terminal scrollback."""

    completions_menu = _completion_menu()
    slash_menu = ConditionalContainer(
        Window(
            content=FormattedTextControl(slash_menu_text),
            height=Dimension(min=1, max=7),
            dont_extend_height=True,
            dont_extend_width=True,
        ),
        Condition(has_slash_menu),
    )
    body = HSplit(
        [
            Window(
                content=_formatted_control(dynamic_text),
                height=Dimension(min=0, max=12),
                dont_extend_height=True,
            ),
            ConditionalContainer(
                Window(
                    content=_formatted_control(todo_text),
                    height=Dimension(min=1, max=10),
                    dont_extend_height=True,
                ),
                Condition(has_todos),
            ),
            Window(height=1, char="─", style="class:border"),
            Window(height=SURFACE_VERTICAL_PADDING, style="class:surface"),
            Window(
                input_control,
                height=Dimension(min=1, max=8),
                wrap_lines=True,
                dont_extend_height=True,
                style="class:surface",
            ),
            Window(height=SURFACE_VERTICAL_PADDING, style="class:surface"),
            slash_menu,
            completions_menu,
            Window(
                content=_formatted_control(status_text),
                height=1,
                align=WindowAlign.LEFT,
                style="class:status",
            ),
        ]
    )
    resolved_output = terminal_output(output)
    application: Application[None] = Application(
        layout=Layout(body, focused_element=input_control),
        key_bindings=key_bindings,
        full_screen=False,
        mouse_support=False,
        input=input,
        output=resolved_output,
        color_depth=terminal_color_depth(resolved_output),
        style=theme.prompt_toolkit_style(),
    )
    return TerminalLayout(application, body, completions_menu, slash_menu)


def _completion_menu() -> ConditionalContainer:
    """Create a completion menu without prompt_toolkit's scrollbar margin."""

    return ConditionalContainer(
        content=Window(
            content=CompletionsMenuControl(),
            width=Dimension(min=8),
            height=Dimension(min=1, max=7),
            scroll_offsets=ScrollOffsets(top=1, bottom=1),
            right_margins=[],
            dont_extend_width=True,
            style="class:completion-menu",
            z_index=10**8,
        ),
        filter=has_completions & ~is_done,
    )


def _formatted_control(
    callback: Callable[[], AnyFormattedText],
) -> FormattedTextControl:
    return FormattedTextControl(lambda: to_formatted_text(callback()))


__all__ = ["TerminalLayout", "build_terminal_layout"]
