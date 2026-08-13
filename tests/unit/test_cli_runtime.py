from pathlib import Path

import pytest

from nano_code.auth import CredentialSource
from nano_code.cli.runtime import (
    CliChatRuntime,
    DeferredPermissionPrompter,
    build_engine,
)
from nano_code.config import NanoCodePaths, Settings
from nano_code.messages import ChatMessage, TextBlock
from nano_code.permissions import PermissionMode
from nano_code.sessions import SessionStore
from nano_code.tui import HistoryAssistantMessage, HistoryUserMessage

_CURRENT_SESSION_ID = "11111111-1111-1111-1111-111111111111"
_TARGET_SESSION_ID = "22222222-2222-2222-2222-222222222222"


def _build_runtime(tmp_path: Path) -> CliChatRuntime:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paths = NanoCodePaths.discover(
        workspace,
        environ={},
        home=tmp_path / "home",
    )
    settings = Settings(
        paths=paths,
        provider_id="anthropic",
        model="test-model",
        permission_mode=PermissionMode.DEFAULT,
        max_turns=3,
        max_output_tokens=1024,
        context_chars=10_000,
        interactive=True,
        credential_source=CredentialSource.NONE,
    )
    prompter = DeferredPermissionPrompter()
    engine = build_engine(
        settings,
        _CURRENT_SESSION_ID,
        permission_prompter=prompter,
    )
    return CliChatRuntime(engine, settings, prompter)


@pytest.mark.asyncio
async def test_runtime_lists_and_atomically_switches_project_session(
    tmp_path: Path,
) -> None:
    runtime = _build_runtime(tmp_path)
    store = SessionStore(
        runtime.settings.paths.project_state_dir,
        _TARGET_SESSION_ID,
    )
    user = ChatMessage(
        role="user",
        origin="human",
        content=(TextBlock("historical question"),),
    )
    assistant = ChatMessage(
        role="assistant",
        origin="model",
        content=(TextBlock("historical answer"),),
        parent_uuid=user.uuid,
    )
    store.append(user)
    store.append(assistant)

    sessions = await runtime.list_sessions()
    resumed = await runtime.resume_session(_TARGET_SESSION_ID)

    assert [session.session_id for session in sessions] == [_TARGET_SESSION_ID]
    assert resumed.status.session_id == _TARGET_SESSION_ID
    assert resumed.status.message_count == 2
    assert resumed.history == (
        HistoryUserMessage("historical question"),
        HistoryAssistantMessage("historical answer"),
    )
    assert runtime.engine.tool_executor.result_store.root == (
        runtime.settings.paths.tool_results_dir(_TARGET_SESSION_ID)
    )
