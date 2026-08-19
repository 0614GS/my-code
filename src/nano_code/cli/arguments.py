"""无文件系统副作用的命令行参数解析。"""

import argparse
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from nano_code import __version__
from nano_code.config.settings import SettingsOverrides
from nano_code.permissions.models import PermissionMode


@dataclass(frozen=True, slots=True)
class CliOptions:
    cwd: Path
    prompt: str | None
    session_id: str | None
    interactive: bool
    settings_overrides: SettingsOverrides


class AuthAction(StrEnum):
    LOGIN = "login"
    STATUS = "status"
    LOGOUT = "logout"


@dataclass(frozen=True, slots=True)
class AuthOptions:
    action: AuthAction
    cwd: Path
    provider_override: str | None


type ParsedOptions = CliOptions | AuthOptions


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nanocode",
        description="A small coding agent for learning Claude Code's architecture.",
    )
    parser.add_argument("-p", "--print", dest="prompt", help="run one prompt and exit")
    parser.add_argument("--cwd", type=Path, default=Path.cwd(), help="workspace root")
    parser.add_argument("--model", help="model override")
    parser.add_argument("--provider", help="named provider profile override")
    parser.add_argument(
        "--base-url",
        help="provider API base URL override",
    )
    parser.add_argument(
        "--permission-mode",
        choices=[mode.value for mode in PermissionMode],
        help="permission mode override",
    )
    parser.add_argument("--max-steps", type=int)
    parser.add_argument("--max-output-tokens", type=int)
    parser.add_argument("--context-chars", type=int)
    parser.add_argument("--session", dest="session_id", help="resume a session ID")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command")
    auth_parser = subparsers.add_parser(
        "auth", help="manage the active provider API key"
    )
    auth_commands = auth_parser.add_subparsers(dest="auth_action", required=True)
    auth_commands.add_parser("login", help="store an API key for future sessions")
    auth_commands.add_parser("status", help="show the active credential source")
    auth_commands.add_parser("logout", help="remove the stored API key")
    return parser


def parse_args(argv: list[str] | None = None) -> CliOptions:
    namespace = build_parser().parse_args(argv)
    if namespace.command is not None:
        raise ValueError("An auth command cannot be parsed as a chat invocation")
    return _parse_chat_options(namespace)


def parse_cli(argv: list[str] | None = None) -> ParsedOptions:
    namespace = build_parser().parse_args(argv)
    if namespace.command == "auth":
        return AuthOptions(
            action=AuthAction(namespace.auth_action),
            cwd=namespace.cwd,
            provider_override=namespace.provider,
        )
    return _parse_chat_options(namespace)


def _parse_chat_options(namespace: argparse.Namespace) -> CliOptions:
    prompt: str | None = namespace.prompt
    interactive = prompt is None and sys.stdin.isatty() and sys.stdout.isatty()
    return CliOptions(
        cwd=namespace.cwd,
        prompt=prompt,
        session_id=namespace.session_id,
        interactive=interactive,
        settings_overrides=SettingsOverrides(
            provider_id=namespace.provider,
            model=namespace.model,
            base_url=namespace.base_url,
            permission_mode=(
                PermissionMode(namespace.permission_mode)
                if namespace.permission_mode is not None
                else None
            ),
            max_steps=namespace.max_steps,
            max_output_tokens=namespace.max_output_tokens,
            context_chars=namespace.context_chars,
        ),
    )


__all__ = [
    "AuthOptions",
    "CliOptions",
    "parse_cli",
]
