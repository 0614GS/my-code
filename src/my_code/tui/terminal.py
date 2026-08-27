"""Terminal output policy for the inline TUI host."""

from __future__ import annotations

import os

from prompt_toolkit.output import Output
from prompt_toolkit.output.color_depth import ColorDepth
from prompt_toolkit.output.defaults import create_output
from prompt_toolkit.output.vt100 import Vt100_Output


class NativeCursorVt100Output(Vt100_Output):
    """VT output which does not override the terminal's blink preference.

    prompt_toolkit's stock implementation emits DEC private mode 12 reset every
    time the renderer shows the cursor.  That explicitly disables blinking and
    is independent from its cursor-shape configuration.
    """

    def show_cursor(self) -> None:
        if self._cursor_visible in (False, None):
            self._cursor_visible = True
            # DEC private mode 12 restores blinking after prompt_toolkit hid
            # the cursor for a redraw.  We deliberately leave DECSCUSR alone,
            # so shape and width still come from the terminal profile.
            self.write_raw("\x1b[?12h\x1b[?25h")


def terminal_output(output: Output | None) -> Output:
    """Return an output preserving native VT cursor shape and blinking."""

    if output is not None:
        return output
    created = create_output(always_prefer_tty=True)
    if not isinstance(created, Vt100_Output):
        return created
    return NativeCursorVt100Output(
        created.stdout,
        created._get_size,
        term=created.term,
        default_color_depth=created.default_color_depth,
        enable_bell=created.enable_bell,
        enable_cpr=created.enable_cpr,
    )


def terminal_color_depth(output: Output) -> ColorDepth:
    """Recognize true-color terminals that still advertise xterm-256color."""

    if os.environ.get("WT_SESSION") or os.environ.get("COLORTERM", "").casefold() in {
        "truecolor",
        "24bit",
    }:
        return ColorDepth.DEPTH_24_BIT
    return output.get_default_color_depth()


def terminal_supports_true_color() -> bool:
    return bool(
        os.environ.get("WT_SESSION")
        or os.environ.get("COLORTERM", "").casefold() in {"truecolor", "24bit"}
    )


__all__ = [
    "NativeCursorVt100Output",
    "terminal_color_depth",
    "terminal_output",
    "terminal_supports_true_color",
]
