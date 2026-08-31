"""Semantic, terminal-friendly styling shared by prompt_toolkit and Rich."""

from __future__ import annotations

import os
import re
import select
import sys
import time
from dataclasses import dataclass

from prompt_toolkit.styles import Style

Rgb = tuple[int, int, int]


@dataclass(frozen=True, slots=True)
class TerminalPalette:
    background: Rgb | None = None

    @classmethod
    def detect(cls) -> TerminalPalette:
        """Use the conventional terminal palette hint when it is available."""

        probed = _probe_osc_background()
        if probed is not None:
            return cls(probed)
        raw = os.environ.get("COLORFGBG", "")
        try:
            background_index = int(raw.rsplit(";", 1)[-1])
        except ValueError:
            return cls()
        ansi = {
            0: (0, 0, 0),
            7: (229, 229, 229),
            8: (127, 127, 127),
            15: (255, 255, 255),
        }
        return cls(ansi.get(background_index))

    @property
    def surface(self) -> str:
        if self.background is None:
            return "default"
        light = _is_light(self.background)
        top = (0, 0, 0) if light else (255, 255, 255)
        mixed = _blend(top, self.background, 0.04 if light else 0.12)
        return f"#{mixed[0]:02x}{mixed[1]:02x}{mixed[2]:02x}"

    @property
    def accent(self) -> str:
        if self.background is not None and _is_light(self.background):
            return "#005f87"
        return "#46a6e8"


@dataclass(frozen=True, slots=True)
class TuiTheme:
    palette: TerminalPalette

    @classmethod
    def detect(cls) -> TuiTheme:
        return cls(TerminalPalette.detect())

    def prompt_toolkit_style(self) -> Style:
        surface = self.palette.surface
        accent = self.palette.accent
        return Style.from_dict(
            {
                "border": "ansibrightblack",
                "status": "ansibrightblack",
                "plan": f"fg:{accent} bold",
                "surface": f"bg:{surface}",
                "prompt": f"fg:{accent} bold",
                "secondary": "ansibrightblack",
                "heading": "bold",
                "selected": f"noinherit fg:{accent} bg:default bold noreverse",
                "success": "ansigreen bold",
                "error": "ansired bold",
                "brand": "ansimagenta bold",
                "completion-menu": "noinherit bg:default fg:default",
                "completion-menu.completion": (
                    "noinherit bg:default fg:default noreverse"
                ),
                "completion-menu.meta.completion": (
                    "noinherit bg:default ansibrightblack noreverse"
                ),
                "completion-menu.completion.current": (
                    f"noinherit fg:{accent} bg:default bold noreverse"
                ),
                "completion-menu.meta.completion.current": (
                    f"noinherit fg:{accent} bg:default bold noreverse"
                ),
            }
        )

    @property
    def rich_surface(self) -> str:
        surface = self.palette.surface
        return "" if surface == "default" else f"on {surface}"


def _blend(top: Rgb, bottom: Rgb, alpha: float) -> Rgb:
    return tuple(
        round(top[index] * alpha + bottom[index] * (1.0 - alpha)) for index in range(3)
    )  # type: ignore[return-value]


def _is_light(color: Rgb) -> bool:
    red, green, blue = color
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue >= 160


def _probe_osc_background(timeout: float = 0.06) -> Rgb | None:
    """Best-effort OSC 11 query before prompt_toolkit owns the terminal."""

    if os.name != "posix" or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None
    import termios

    fd = sys.stdin.fileno()
    previous: list[int | list[bytes | int]] | None = None
    try:
        previous = termios.tcgetattr(fd)
        changed = termios.tcgetattr(fd)
        changed[3] &= ~(termios.ICANON | termios.ECHO)
        changed[6][termios.VMIN] = 0
        changed[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, changed)
        sys.stdout.write("\x1b]11;?\x1b\\")
        sys.stdout.flush()
        deadline = time.monotonic() + timeout
        response = b""
        while time.monotonic() < deadline and len(response) < 256:
            ready, _, _ = select.select([fd], [], [], deadline - time.monotonic())
            if not ready:
                break
            response += os.read(fd, 256 - len(response))
            if b"\x07" in response or b"\x1b\\" in response:
                break
    except (OSError, termios.error, ValueError):
        return None
    finally:
        if previous is not None:
            try:
                termios.tcsetattr(fd, termios.TCSANOW, previous)
            except (OSError, termios.error):
                pass
    match = re.search(
        rb"\x1b\]11;rgb:([0-9a-fA-F]{2,4})/([0-9a-fA-F]{2,4})/([0-9a-fA-F]{2,4})",
        response,
    )
    if match is None:
        return None
    values = tuple(int(value[:2], 16) for value in match.groups())
    return values  # type: ignore[return-value]


__all__ = ["TerminalPalette", "TuiTheme"]
