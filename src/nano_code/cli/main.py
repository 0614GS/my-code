"""Console entry point and minimal REPL."""

import asyncio
import sys
from collections.abc import Coroutine
from typing import Any

from nano_code.agent import AgentEngine
from nano_code.cli.arguments import CliOptions, parse_args
from nano_code.cli.runtime import build_engine


async def _submit(engine: AgentEngine, prompt: str) -> None:
    result = await engine.submit(prompt)
    print(result.text or "<no text response>")


async def _repl(engine: AgentEngine, session_id: str) -> None:
    print(f"nano-code session {session_id}. Type /exit to quit.")
    while True:
        try:
            # The MVP event loop has no concurrent UI work, and direct input also works
            # in restricted environments where Python worker threads are unavailable.
            prompt = input("nano-code> ")  # noqa: ASYNC250
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if prompt.strip() in {"/exit", "/quit"}:
            return
        if not prompt.strip():
            continue
        try:
            await _submit(engine, prompt)
        except KeyboardInterrupt:
            print("Cancelled.", file=sys.stderr)
        except Exception as error:
            print(f"Error: {error}", file=sys.stderr)


async def run(options: CliOptions) -> int:
    engine = build_engine(options.settings, options.session_id)
    if options.prompt is not None:
        await _submit(engine, options.prompt)
        return 0
    await _repl(engine, engine.session_store.session_id)
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
        options = parse_args(argv)
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
    raise SystemExit(_run_async(run(options)))
