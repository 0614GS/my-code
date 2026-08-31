import asyncio

import pytest

from my_code.application.turns.permission_prompt import DeferredPermissionPrompter
from my_code.permissions.models import (
    PermissionBehavior,
    PermissionConfirmation,
    PermissionDecision,
    PermissionDecisionKind,
    PermissionDecisionReason,
    PermissionPrompt,
)


def _prompt() -> PermissionPrompt:
    return PermissionPrompt(
        tool_name="Write",
        tool_input={"path": "a.txt"},
        decision=PermissionDecision(
            PermissionBehavior.ASK,
            "approve",
            PermissionDecisionReason(PermissionDecisionKind.TOOL, "write"),
        ),
        display_name="Write",
        summary="Write a.txt",
        activity="Writing",
    )


@pytest.mark.asyncio
async def test_pending_permission_is_released_on_runtime_close() -> None:
    prompter = DeferredPermissionPrompter()
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(_request):
        started.set()
        await release.wait()
        return PermissionConfirmation(True)

    prompter.set_handler(handler)
    pending = asyncio.create_task(prompter.confirm(_prompt()))
    await started.wait()

    assert prompter.pending_count == 1
    await prompter.close()

    assert pending.cancelled()
    assert prompter.pending_count == 0


@pytest.mark.asyncio
async def test_failed_permission_handler_releases_pending_state() -> None:
    prompter = DeferredPermissionPrompter()

    async def handler(_request):
        raise RuntimeError("host failed")

    prompter.set_handler(handler)
    with pytest.raises(RuntimeError, match="host failed"):
        await prompter.confirm(_prompt())

    assert prompter.pending_count == 0


@pytest.mark.asyncio
async def test_permission_prompts_are_serialized_across_callers() -> None:
    prompter = DeferredPermissionPrompter()
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    calls: list[str] = []

    async def handler(request):
        calls.append(request.tool_input["path"])
        if len(calls) == 1:
            first_started.set()
            await release_first.wait()
        return PermissionConfirmation(True)

    prompter.set_handler(handler)
    first = asyncio.create_task(prompter.confirm(_prompt()))
    await first_started.wait()
    second_prompt = _prompt()
    second_prompt = PermissionPrompt(
        second_prompt.tool_name,
        {"path": "b.txt"},
        second_prompt.decision,
        second_prompt.display_name,
        second_prompt.summary,
        second_prompt.activity,
    )
    second = asyncio.create_task(prompter.confirm(second_prompt))
    await asyncio.sleep(0)

    assert calls == ["a.txt"]
    assert prompter.pending_count == 2
    release_first.set()
    await asyncio.gather(first, second)

    assert calls == ["a.txt", "b.txt"]
    assert prompter.pending_count == 0
