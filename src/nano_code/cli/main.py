"""控制台入口与最小化 REPL。"""

import asyncio
import sys
from collections.abc import Coroutine
from typing import Any

from nano_code.agent import AgentEngine
from nano_code.cli.arguments import AuthOptions, CliOptions, parse_cli
from nano_code.cli.auth import run_auth_command
from nano_code.cli.runtime import (
    DeferredPermissionPrompter,
    build_engine,
    build_runtime,
)
from nano_code.config import bootstrap_user_storage
from nano_code.tui import NanoCodeTui


async def _submit(engine: AgentEngine, prompt: str) -> None:
    result = await engine.submit(prompt)
    print(result.text or "<no text response>")


async def run(options: CliOptions) -> int:
    if options.prompt is not None:
        engine = build_engine(options.settings, options.session_id)
        await _submit(engine, options.prompt)
        return 0
    permission_prompter = DeferredPermissionPrompter()
    runtime = build_runtime(
        options.settings,
        options.session_id,
        permission_prompter=permission_prompter,
    )
    await NanoCodeTui(runtime).run()
    return 0


def _run_async(task: Coroutine[Any, Any, int]) -> int:
    try:
        return asyncio.run(task)
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> None:
    try:
        options = parse_cli(argv)
        paths = (
            options.paths
            if isinstance(options, AuthOptions)
            else options.settings.paths
        )
        bootstrap_user_storage(paths)
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
    if isinstance(options, AuthOptions):
        try:
            raise SystemExit(run_auth_command(options))
        except (EOFError, KeyboardInterrupt):
            print("Cancelled.", file=sys.stderr)
            raise SystemExit(130) from None
        except ValueError as error:
            print(f"Error: {error}", file=sys.stderr)
            raise SystemExit(2) from error
    raise SystemExit(_run_async(run(options)))
