from pathlib import Path

import pytest

from nano_code.auth import CredentialSource
from nano_code.cli.runtime import (
    CliChatRuntime,
    DeferredPermissionPrompter,
    build_runtime,
)
from nano_code.config import NanoCodePaths, Settings
from nano_code.messages import (
    AssistantMessage,
    HumanMessage,
    TextContent,
    TokenUsage,
    ToolCall,
    ToolResult,
    ToolResultsMessage,
)
from nano_code.permissions import PermissionMode
from nano_code.presentation import ToolResultPresentation, ToolUsePresentation
from nano_code.sessions import SessionStore
from nano_code.tui import (
    HistoryAssistantMessage,
    HistoryToolCall,
    HistoryUserMessage,
)

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
    return build_runtime(
        settings,
        _CURRENT_SESSION_ID,
        permission_prompter=prompter,
    )


@pytest.mark.asyncio
async def test_runtime_lists_and_atomically_switches_project_session(
    tmp_path: Path,
) -> None:
    runtime = _build_runtime(tmp_path)
    store = SessionStore(
        runtime.settings.paths.project_state_dir,
        _TARGET_SESSION_ID,
    )
    user = HumanMessage(content="historical question")
    assistant = AssistantMessage(
        content=(TextContent("historical answer"),),
        usage=TokenUsage(),
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
    assert runtime.status().session_id == _TARGET_SESSION_ID


@pytest.mark.asyncio
async def test_resume_uses_persisted_tool_presentation_snapshot(tmp_path: Path) -> None:
    runtime = _build_runtime(tmp_path)
    store = SessionStore(
        runtime.settings.paths.project_state_dir,
        _TARGET_SESSION_ID,
    )
    user = HumanMessage(content="read it")
    assistant = AssistantMessage(
        content=(ToolCall("read-1", "Read", {"path": "old.py"}),),
        usage=TokenUsage(),
        parent_uuid=user.uuid,
    )
    snapshot = ToolResultPresentation(
        summary="Historical read summary",
        detail="Stored at execution time",
    )
    result = ToolResultsMessage(
        content=(
            ToolResult(
                "read-1",
                "model-visible historical content",
                presentation=snapshot,
            ),
        ),
        parent_uuid=assistant.uuid,
        source_assistant_uuid=assistant.uuid,
    )
    for message in (user, assistant, result):
        store.append(message)

    resumed = await runtime.resume_session(_TARGET_SESSION_ID)

    assert resumed.history == (
        HistoryUserMessage("read it"),
        HistoryToolCall(
            tool_use_id="read-1",
            use=ToolUsePresentation(
                display_name="Read",
                summary="old.py",
                activity="Reading old.py",
            ),
            result=snapshot,
            is_error=False,
        ),
    )
