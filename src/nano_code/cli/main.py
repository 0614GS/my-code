"""控制台入口与进程生命周期。"""

import asyncio
import sys
from collections.abc import Coroutine
from typing import Any

from nano_code.agent import AgentMaxTurnsReached, AgentTurnSucceeded
from nano_code.cli.arguments import AuthOptions, CliOptions, parse_cli
from nano_code.cli.auth import run_auth_command
from nano_code.core import SettingsResolver
from nano_code.core.bootstrap import (
    bootstrap_agent,
    bootstrap_cli_runtime,
    initialize_user_storage,
)
from nano_code.tui import NanoCodeTui


async def _submit(options: CliOptions, resolver: SettingsResolver) -> int:
    settings = resolver.resolve(
        options.settings_overrides,
        interactive=options.interactive,
    )
    agent = bootstrap_agent(settings, options.session_id)
    result = await agent.submit(options.prompt or "")
    if isinstance(result, AgentTurnSucceeded):
        print(result.text or "<no text response>")
        return 0
    assert isinstance(result, AgentMaxTurnsReached)
    print(f"Error: Reached max turns ({result.max_turns})", file=sys.stderr)
    return 1


async def run(options: CliOptions, resolver: SettingsResolver) -> int:
    if options.prompt is not None:
        return await _submit(options, resolver)
    settings = resolver.resolve(
        options.settings_overrides,
        interactive=options.interactive,
    )
    runtime = bootstrap_cli_runtime(settings, options.session_id)
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
        resolver = SettingsResolver.for_workspace(options.cwd)
        initialize_user_storage(resolver.paths)
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
    if isinstance(options, AuthOptions):
        provider_id = resolver.active_provider_id(options.provider_override)
        try:
            raise SystemExit(run_auth_command(options, resolver.paths, provider_id))
        except (EOFError, KeyboardInterrupt):
            print("Cancelled.", file=sys.stderr)
            raise SystemExit(130) from None
        except ValueError as error:
            print(f"Error: {error}", file=sys.stderr)
            raise SystemExit(2) from error
    raise SystemExit(_run_async(run(options, resolver)))
