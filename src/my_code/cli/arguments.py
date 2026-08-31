"""无文件系统副作用的命令行参数解析。"""

import argparse
from dataclasses import dataclass
from pathlib import Path

from my_code.config.settings import SettingsOverrides
from my_code.permissions.models import PermissionMode
from my_code.version import __version__


@dataclass(frozen=True, slots=True)
class CliOptions:
    cwd: Path
    session_id: str | None
    settings_overrides: SettingsOverrides


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mycode",
        description=(
            "A modular, glass-box coding agent that shows what the model sees."
        ),
    )
    parser.add_argument("--cwd", type=Path, default=Path.cwd(), help="workspace root")
    parser.add_argument("--provider", help="named provider profile override")
    parser.add_argument(
        "--permission-mode",
        choices=[mode.value for mode in PermissionMode],
        help="permission mode override",
    )
    parser.add_argument("--max-steps", type=int)
    parser.add_argument("--max-output-tokens", type=int)
    parser.add_argument("--session", dest="session_id", help="resume a session ID")
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def parse_args(argv: list[str] | None = None) -> CliOptions:
    return parse_cli(argv)


def parse_cli(argv: list[str] | None = None) -> CliOptions:
    namespace = build_parser().parse_args(argv)
    return CliOptions(
        cwd=namespace.cwd,
        session_id=namespace.session_id,
        settings_overrides=SettingsOverrides(
            provider_id=namespace.provider,
            permission_mode=(
                PermissionMode(namespace.permission_mode)
                if namespace.permission_mode is not None
                else None
            ),
            max_steps=namespace.max_steps,
            max_output_tokens=namespace.max_output_tokens,
        ),
    )


__all__ = [
    "CliOptions",
    "parse_cli",
]
