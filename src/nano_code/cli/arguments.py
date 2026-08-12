"""Command-line parsing without side effects."""

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from nano_code import __version__
from nano_code.config import Settings
from nano_code.permissions import PermissionMode

_DEFAULT_MODEL = "claude-sonnet-4-6"


@dataclass(frozen=True, slots=True)
class CliOptions:
    settings: Settings
    prompt: str | None
    session_id: str | None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nano-code",
        description="A small coding agent for learning Claude Code's architecture.",
    )
    parser.add_argument("-p", "--print", dest="prompt", help="run one prompt and exit")
    parser.add_argument("--cwd", type=Path, default=Path.cwd(), help="workspace root")
    parser.add_argument("--model", default=os.getenv("ANTHROPIC_MODEL", _DEFAULT_MODEL))
    parser.add_argument(
        "--permission-mode",
        choices=[mode.value for mode in PermissionMode],
        default=PermissionMode.DEFAULT.value,
    )
    parser.add_argument("--max-turns", type=int, default=12)
    parser.add_argument("--max-output-tokens", type=int, default=8192)
    parser.add_argument("--context-chars", type=int, default=160_000)
    parser.add_argument("--session", dest="session_id", help="resume a session ID")
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def parse_args(argv: list[str] | None = None) -> CliOptions:
    namespace = build_parser().parse_args(argv)
    cwd = namespace.cwd.resolve()
    if not cwd.is_dir():
        raise ValueError(f"Workspace is not a directory: {cwd}")
    prompt: str | None = namespace.prompt
    interactive = prompt is None and sys.stdin.isatty() and sys.stdout.isatty()
    settings = Settings(
        cwd=cwd,
        model=namespace.model,
        permission_mode=PermissionMode(namespace.permission_mode),
        max_turns=namespace.max_turns,
        max_output_tokens=namespace.max_output_tokens,
        context_chars=namespace.context_chars,
        state_dir=cwd / ".nano-code",
        interactive=interactive,
        api_key=os.getenv("ANTHROPIC_API_KEY"),
        base_url=os.getenv("ANTHROPIC_BASE_URL"),
    )
    return CliOptions(settings=settings, prompt=prompt, session_id=namespace.session_id)
