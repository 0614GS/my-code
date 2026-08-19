import asyncio

import pytest

from my_code.chat.permissions import DeferredPermissionPrompter
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
