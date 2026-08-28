"""Terminal output policy for the inline TUI host."""

from __future__ import annotations

import os
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
from prompt_toolkit.keys import Keys
from prompt_toolkit.output import Output
from prompt_toolkit.output.color_depth import ColorDepth
from prompt_toolkit.output.defaults import create_output
from prompt_toolkit.output.vt100 import Vt100_Output

_KEY_SEQUENCE_TIMEOUT_SECONDS = 0.05

# prompt_toolkit 3.x does not expose a Shift+Enter key name.  Translate the
# two enhanced-terminal encodings into Control-J, which is our unambiguous
# composer newline action.  Terminals that send plain CR for Shift+Enter
# cannot be distinguished from Enter; Control-J remains the portable fallback.
ANSI_SEQUENCES["\x1b[13;2u"] = Keys.ControlJ
ANSI_SEQUENCES["\x1b[27;2;13~"] = Keys.ControlJ


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


def configure_key_timeouts(application: Application[Any]) -> None:
    """Keep a bare Escape responsive while retaining atomic VT sequences."""

    application.ttimeoutlen = _KEY_SEQUENCE_TIMEOUT_SECONDS
    application.timeoutlen = 0.0


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
    "configure_key_timeouts",
    "terminal_color_depth",
    "terminal_output",
    "terminal_supports_true_color",
]
