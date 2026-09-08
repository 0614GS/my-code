"""从真实工具错误到后续请求及离线报告的完整证据链。"""

import json
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

from my_code.agent.engine import AgentEngine
from my_code.agent.models import AgentMaxStepsReached, AgentTurnInput
from my_code.context.compaction import ContextCompactor
from my_code.context.engine import ContextEngine
from my_code.context.planner import ContextPlanner
from my_code.context.session_cache import SessionContextCache
from my_code.model.events import (
    ModelStreamEvent,
    ModelStreamSequencer,
    completed_output_payloads,
)
from my_code.model.primitives import ProviderBinding, TokenUsage
from my_code.model.request import (
    ModelOutput,
    ModelRequest,
    ModelToolUseBlock,
    PromptStability,
)
from my_code.observability.api import NoOpTracer
from my_code.observability.diagnostic_log import JsonlObservationSink
from my_code.observability.dispatcher import ObservationDispatcher
from my_code.permissions.models import PermissionMode
from my_code.permissions.policy import PermissionPolicy
from my_code.permissions.prompt import HeadlessPrompter
from my_code.prompts.models import PromptSection
from my_code.prompts.registry import PromptRegistry
from my_code.runtime.instrumentation import (
    InstrumentedAgentRunner,
    InstrumentedModelClient,
    InstrumentedToolExecutor,
)
from my_code.sessions.diagnostics import build_diagnostic_report, request_evidence
from my_code.sessions.inspection import inspect_session
from my_code.sessions.session import Session
from my_code.tools.builtin import builtin_tools
from my_code.tools.catalog import ToolCatalog, ToolSourceId
from my_code.tools.executor import ToolExecutor
from my_code.tools.round_executor import ToolRoundExecutor
from my_code.workspace.local import Workspace


class _RepeatingModel:
    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        output = ModelOutput(
            (
                ModelToolUseBlock(
                    str(uuid4()), "Read", {"path": "missing-private-file"}
                ),
            ),
            "tool_use",
            TokenUsage(2, 3, 4, 5, True),
        )
        sequencer = ModelStreamSequencer()
        for payload in completed_output_payloads(output):
            yield sequencer.emit(payload)


async def test_tool_error_repetition_links_to_exact_requests_without_otel(
    tmp_path: Path,
) -> None:
    diagnostics = JsonlObservationSink(tmp_path / "diagnostics.jsonl")
    observations = ObservationDispatcher(NoOpTracer(), (diagnostics,))
    model = InstrumentedModelClient(
        _RepeatingModel(),
        observations,
        lambda: ProviderBinding("test", "provider", "model"),
        purpose="agent",
    )
    catalog = ToolCatalog()
    catalog.register_source(ToolSourceId("test", "integration"), builtin_tools())
    executor = ToolExecutor(
        tools=catalog.snapshot(),
        policy=PermissionPolicy(PermissionMode.BYPASS),
        prompter=HeadlessPrompter(),
        workspace=Workspace(tmp_path),
    )
    planner = ContextPlanner(
        prompt=PromptRegistry(
            (PromptSection("core", PromptStability.STATIC, lambda: "system"),)
        ),
        max_output_tokens=100,
    )
    runner = InstrumentedAgentRunner(
        AgentEngine(
            model_call=model,
            tool_round=ToolRoundExecutor(
                InstrumentedToolExecutor(executor, observations)
            ),
            context=ContextEngine(planner, ContextCompactor(model)),
            tool_catalog=catalog,
            max_steps=3,
        ),
        observations,
    )
    session = Session(tmp_path / "sessions", str(uuid4()))
    outcome = await runner.submit(
        session, SessionContextCache(), AgentTurnInput("read the file")
    )
    diagnostics.close()
    assert isinstance(outcome, AgentMaxStepsReached)

    snapshot = inspect_session(tmp_path / "sessions", session.session_id)
    report = build_diagnostic_report(snapshot, include_content=True)
    findings = report["findings"]
    assert isinstance(findings, list)
    errors = [
        item
        for item in findings
        if isinstance(item, dict) and item["kind"] == "tool_error"
    ]
    assert len(errors) == 3
    assert "repeated_tool_rounds" in json.dumps(report)
    assert errors[0]["requests_consuming_result"] == [
        item.manifest.request_id for item in snapshot.audit.requests[1:]
    ]
    evidence = request_evidence(
        snapshot, snapshot.audit.requests[1].manifest.request_id
    )
    assert "missing-private-file" in json.dumps(evidence)

    records = [json.loads(line) for line in diagnostics.path.read_text().splitlines()]
    responses = [item for item in records if item["event"] == "model.response.received"]
    assert len(responses) == 3
    assert [item["request_id"] for item in responses] == [
        item.manifest.request_id for item in snapshot.audit.requests
    ]
    assert all(
        item["cache_read_tokens"] == 5 and item["cache_creation_tokens"] == 4
        for item in responses
    )
    assert all(item["session_id"] == session.session_id for item in records)
    assert all(
        item["invocation_id"] == session.invocation_history[0].started.invocation_id
        for item in records
    )
    assert "missing-private-file" not in diagnostics.path.read_text()
    assert records[-1]["outcome"] == "limit"
