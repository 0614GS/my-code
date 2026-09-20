"""无文件系统副作用的命令行参数解析。"""

import argparse
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from my_code.config.settings import SettingsOverrides
from my_code.permissions.models import PermissionMode
from my_code.version import __version__


@dataclass(frozen=True, slots=True)
class CliOptions:
    cwd: Path
    session_id: str | None
    settings_overrides: SettingsOverrides


class OutputFormat(StrEnum):
    TEXT = "text"
    JSON = "json"
    STREAM_JSON = "stream-json"


@dataclass(frozen=True, slots=True)
class RunCliOptions:
    cwd: Path
    session_id: str | None
    settings_overrides: SettingsOverrides
    prompt: str | None
    output_format: OutputFormat
    timeout_seconds: float | None
    dangerously_skip_permissions: bool


type ParsedCliOptions = CliOptions | RunCliOptions


def _add_runtime_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cwd", type=Path, default=Path.cwd(), help="workspace root")
    parser.add_argument("--provider", help="named provider profile override")
    parser.add_argument("--max-steps", type=int)
    parser.add_argument("--max-output-tokens", type=int)
    parser.add_argument("--session", dest="session_id", help="resume a session ID")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mycode",
        description=(
            "A modular, glass-box coding agent that shows what the model sees."
        ),
    )
    _add_runtime_options(parser)
    parser.add_argument(
        "--permission-mode",
        choices=[mode.value for mode in PermissionMode],
        help="permission mode override",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command")
    run = subparsers.add_parser(
        "run",
        help="run one prompt without an interactive frontend",
        description="Run one agent turn without an interactive frontend.",
    )
    _add_runtime_options(run)
    run.add_argument("prompt", nargs="?", help="prompt; reads stdin when omitted")
    run.add_argument(
        "--output-format",
        choices=[item.value for item in OutputFormat],
        default=OutputFormat.TEXT.value,
    )
    run.add_argument("--timeout-seconds", type=float)
    permission = run.add_mutually_exclusive_group()
    permission.add_argument(
        "--permission-mode",
        choices=[
            PermissionMode.DEFAULT.value,
            PermissionMode.ACCEPT_EDITS.value,
            PermissionMode.PLAN.value,
            PermissionMode.DONT_ASK.value,
        ],
    )
    permission.add_argument(
        "--dangerously-skip-permissions",
        action="store_true",
        help="bypass ordinary permission prompts; caller owns process isolation",
    )
    run.add_argument(
        "--sandbox-mode",
        choices=["auto", "local"],
        help="command sandbox override",
    )
    run.add_argument(
        "--sandbox-network",
        choices=["restricted", "enabled"],
        help="network policy for the built-in command sandbox",
    )
    return parser


def parse_args(argv: list[str] | None = None) -> ParsedCliOptions:
    return parse_cli(argv)


def parse_cli(argv: list[str] | None = None) -> ParsedCliOptions:
    namespace = build_parser().parse_args(argv)
    permission_mode = (
        PermissionMode.BYPASS
        if getattr(namespace, "dangerously_skip_permissions", False)
        else PermissionMode(namespace.permission_mode)
        if namespace.permission_mode is not None
        else PermissionMode.DONT_ASK
        if namespace.command == "run"
        else None
    )
    overrides = SettingsOverrides(
        provider_id=namespace.provider,
        permission_mode=permission_mode,
        max_steps=namespace.max_steps,
        max_output_tokens=namespace.max_output_tokens,
        sandbox_mode=getattr(namespace, "sandbox_mode", None),
        sandbox_network=getattr(namespace, "sandbox_network", None),
    )
    if namespace.command == "run":
        if namespace.timeout_seconds is not None and namespace.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        return RunCliOptions(
            cwd=namespace.cwd,
            session_id=namespace.session_id,
            settings_overrides=overrides,
            prompt=namespace.prompt,
            output_format=OutputFormat(namespace.output_format),
            timeout_seconds=namespace.timeout_seconds,
            dangerously_skip_permissions=namespace.dangerously_skip_permissions,
        )
    return CliOptions(
        cwd=namespace.cwd,
        session_id=namespace.session_id,
        settings_overrides=overrides,
    )


__all__ = [
    "CliOptions",
    "OutputFormat",
    "ParsedCliOptions",
    "RunCliOptions",
    "parse_cli",
]
