import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from nano_code.agent import (
    AgentEngine,
    AgentTextDelta,
    AgentToolFinished,
    AgentToolStarted,
    AgentTurnCompleted,
)
from nano_code.context import ContextWindow
from nano_code.messages import (
    ChatMessage,
    ModelResponse,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from nano_code.permissions import PermissionMode, PermissionPolicy
from nano_code.permissions.prompt import HeadlessPrompter
from nano_code.providers import (
    ModelRequest,
    ModelResponseCompleted,
    ModelTextDelta,
)
from nano_code.providers.events import ModelStreamEvent
from nano_code.sessions import SessionStore
from nano_code.tools import ToolContext, ToolRegistry
from nano_code.tools.builtin import builtin_tools
from nano_code.tools.executor import ToolExecutor
from nano_code.tools.result_store import ToolResultStore

_SESSION_ID = "12345678-1234-1234-1234-123456789abc"


class FakeProvider:
    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = responses
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return self.responses.pop(0)

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        self.requests.append(request)
        response = self.responses.pop(0)
        for block in response.content:
            if isinstance(block, TextBlock):
                midpoint = max(1, len(block.text) // 2)
                yield ModelTextDelta(block.text[:midpoint])
                yield ModelTextDelta(block.text[midpoint:])
        yield ModelResponseCompleted(response)


def build_engine(tmp_path: Path, provider: FakeProvider) -> AgentEngine:
    store = SessionStore(tmp_path / "state", _SESSION_ID)
    registry = ToolRegistry(builtin_tools())
    executor = ToolExecutor(
        registry=registry,
        policy=PermissionPolicy(PermissionMode.DEFAULT),
        prompter=HeadlessPrompter(),
        context=ToolContext(cwd=tmp_path),
        result_store=ToolResultStore(store.session_dir / "tool-results"),
    )
    return AgentEngine(
        provider=provider,
        tool_executor=executor,
        session_store=store,
        context_window=ContextWindow(),
        system_prompt="test",
    )


@pytest.mark.asyncio
async def test_runs_tool_round_and_persists_protocol_pair(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("hello\n", encoding="utf-8")
    provider = FakeProvider(
        [
            ModelResponse(
                content=(
                    ToolUseBlock(
                        id="tool-1",
                        name="Read",
                        input={"path": "hello.txt"},
                    ),
                ),
                stop_reason="tool_use",
            ),
            ModelResponse(content=(TextBlock("done"),), stop_reason="end_turn"),
        ]
    )
    engine = build_engine(tmp_path, provider)

    result = await engine.submit("read it")

    assert result.text == "done"
    assert result.turns == 2
    assert len(provider.requests) == 2
    second_request = provider.requests[1]
    tool_result = second_request.messages[-1].content[0]
    assert tool_result.type == "tool_result"
    assert tool_result.tool_use_id == "tool-1"
    assert "hello" in tool_result.content
    assert len(engine.session_store.load()) == 4


@pytest.mark.asyncio
async def test_stream_exposes_text_and_tool_lifecycle_in_order(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("hello\n", encoding="utf-8")
    provider = FakeProvider(
        [
            ModelResponse(
                content=(ToolUseBlock("tool-1", "Read", {"path": "hello.txt"}),),
                stop_reason="tool_use",
            ),
            ModelResponse(content=(TextBlock("done"),), stop_reason="end_turn"),
        ]
    )
    engine = build_engine(tmp_path, provider)

    events = [event async for event in engine.submit_stream("read it")]

    assert [type(event) for event in events] == [
        AgentToolStarted,
        AgentToolFinished,
        AgentTextDelta,
        AgentTextDelta,
        AgentTurnCompleted,
    ]
    assert (
        "".join(event.text for event in events if isinstance(event, AgentTextDelta))
        == "done"
    )
    assert len(engine.session_store.load()) == 4


@pytest.mark.asyncio
async def test_headless_write_ask_is_returned_as_denied_result(tmp_path: Path) -> None:
    provider = FakeProvider(
        [
            ModelResponse(
                content=(
                    ToolUseBlock(
                        id="tool-write",
                        name="Write",
                        input={"path": "created.txt", "content": "unsafe"},
                    ),
                ),
                stop_reason="tool_use",
            ),
            ModelResponse(content=(TextBlock("denied"),), stop_reason="end_turn"),
        ]
    )
    engine = build_engine(tmp_path, provider)

    result = await engine.submit("write it")

    assert result.text == "denied"
    assert not (tmp_path / "created.txt").exists()
    denied = provider.requests[1].messages[-1].content[0]
    assert denied.type == "tool_result"
    assert denied.tool_use_id == "tool-write"
    assert denied.is_error is True
    assert "approval was not provided" in denied.content


@pytest.mark.asyncio
async def test_cancellation_persists_results_for_every_tool_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = FakeProvider(
        [
            ModelResponse(
                content=(
                    ToolUseBlock("first", "Read", {"path": "a.txt"}),
                    ToolUseBlock("second", "Read", {"path": "b.txt"}),
                ),
                stop_reason="tool_use",
            )
        ]
    )
    engine = build_engine(tmp_path, provider)

    async def cancel(_call: ToolUseBlock) -> ToolResultBlock:
        raise asyncio.CancelledError

    monkeypatch.setattr(engine.tool_executor, "execute", cancel)

    with pytest.raises(asyncio.CancelledError):
        await engine.submit("read both")

    persisted = engine.session_store.load()
    assert all(isinstance(result, ToolResultBlock) for result in persisted[-1].content)
    tool_results = [
        result
        for result in persisted[-1].content
        if isinstance(result, ToolResultBlock)
    ]
    assert [result.tool_use_id for result in tool_results] == ["first", "second"]
    assert all(result.type == "tool_result" for result in tool_results)
    assert all(result.is_error for result in tool_results)


def test_resume_repairs_trailing_unresolved_tool_uses(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "state", _SESSION_ID)
    user_message = ChatMessage(
        role="user", origin="human", content=(TextBlock("read"),)
    )
    assistant_message = ChatMessage(
        role="assistant",
        origin="model",
        content=(ToolUseBlock("interrupted", "Read", {"path": "a.txt"}),),
        parent_uuid=user_message.uuid,
    )
    store.append(user_message)
    store.append(assistant_message)

    engine = build_engine(tmp_path, FakeProvider([]))

    persisted = engine.session_store.load()
    assert len(persisted) == 3
    repair = persisted[-1].content[0]
    assert isinstance(repair, ToolResultBlock)
    assert repair.tool_use_id == "interrupted"
    assert repair.is_error is True
