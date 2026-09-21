"""Harbor job 聚合保留原始证据并生成稳定报告产物。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from uuid import uuid4

from my_code.conversation.models import (
    AssistantMessage,
    HumanMessage,
    ToolCall,
    ToolResult,
)
from my_code.conversation.presentation import generic_tool_result_presentation
from my_code.model.primitives import TokenUsage
from my_code.sessions.session import Session

_ANALYZER_PATH = Path(__file__).parents[2] / "integrations" / "harbor" / "analyzer.py"
_SPEC = importlib.util.spec_from_file_location("tested_harbor_analyzer", _ANALYZER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_ANALYZER = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _ANALYZER
_SPEC.loader.exec_module(_ANALYZER)
analyze_harbor_job = _ANALYZER.analyze_harbor_job
write_report = _ANALYZER.write_report


def test_job_analyzer_uses_canonical_usage_and_writes_reports(tmp_path: Path) -> None:
    job = tmp_path / "job"
    trial = job / "case__attempt"
    project = trial / "agent" / "mycode" / "projects" / "-testbed"
    project.mkdir(parents=True)
    session = Session(project, str(uuid4()))
    session.append_human_message(HumanMessage("private task"))
    call = ToolCall("bash-1", "Bash", {"command": "private command"})
    assistant = AssistantMessage(
        (call,), TokenUsage(10, 3, 2, 8, True), parent_uuid=session.causal_head_uuid
    )
    session.append_assistant_message(assistant)
    session.append_tool_results(
        (
            ToolResult(
                call.id,
                "exit_code: 1\nprivate failure",
                generic_tool_result_presentation("failed", True),
                True,
            ),
        ),
        assistant,
    )
    diagnostics = project / "diagnostics"
    diagnostics.mkdir()
    (diagnostics / "run.jsonl").write_text(
        json.dumps(
            {
                "version": 2,
                "event": "tool.execution.finished",
                "session_id": session.session_id,
                "tool_call_id": call.id,
                "tool_name": "Bash",
                "duration_ms": 12.5,
                "is_error": True,
                "outcome": "error",
                "timestamp": "2026-01-01T00:00:01+00:00",
                "sequence": 1,
                "dropped_events": 0,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (trial / "agent" / "result.json").write_text(
        json.dumps(
            {
                "outcome": "succeeded",
                "session_id": session.session_id,
                "completed_steps": 1,
                "usage": {
                    "input_tokens": 20,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "total_input_tokens": 20,
                    "output_tokens": 3,
                },
            }
        ),
        encoding="utf-8",
    )
    (trial / "result.json").write_text(
        json.dumps(
            {
                "task_name": "dataset/case",
                "trial_name": trial.name,
                "task_checksum": "checksum",
                "source": "dataset",
                "verifier_result": {"rewards": {"reward": 0}},
            }
        ),
        encoding="utf-8",
    )

    report = analyze_harbor_job(job)
    paths = write_report(report, job / "analysis")

    assert report["summary"]["usage"]["total_input_tokens"] == 20
    assert report["summary"]["usage"]["cache_read_input_tokens"] == 8
    assert report["summary"]["usage"]["cache_hit_ratio"] == 0.4
    assert report["aggregates"]["by_error_category"] == {"command_nonzero": 1}
    assert report["evidence_quality"]["usage_mismatch_trials"] == 1
    assert all(path.is_file() for path in paths)
    assert "private command" not in paths[0].read_text(encoding="utf-8")


def test_job_analyzer_falls_back_to_atif_with_explicit_gap(tmp_path: Path) -> None:
    job = tmp_path / "job"
    trial = job / "case__attempt"
    agent = trial / "agent"
    agent.mkdir(parents=True)
    (trial / "result.json").write_text(
        json.dumps(
            {
                "task_name": "dataset/case",
                "trial_name": trial.name,
                "verifier_result": {"rewards": {"reward": 0}},
            }
        ),
        encoding="utf-8",
    )
    (agent / "result.json").write_text(
        json.dumps({"outcome": "succeeded", "session_id": str(uuid4())}),
        encoding="utf-8",
    )
    (agent / "trajectory.json").write_text(
        json.dumps(
            {
                "schema_version": "ATIF-v1.7",
                "session_id": "fallback-session",
                "steps": [
                    {
                        "source": "agent",
                        "metrics": {
                            "prompt_tokens": 10,
                            "completion_tokens": 2,
                            "cached_tokens": 4,
                            "extra": {"cache_write_tokens": 1},
                        },
                        "tool_calls": [
                            {
                                "tool_call_id": "read-1",
                                "function_name": "Read",
                                "arguments": {"path": 1},
                            }
                        ],
                        "observation": {
                            "results": [
                                {
                                    "source_call_id": "read-1",
                                    "content": "Invalid input: 'path' must be a string",
                                    "extra": {"is_error": True},
                                }
                            ]
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    report = analyze_harbor_job(job)
    analyzed = report["trials"][0]

    assert analyzed["evidence_source"] == "atif"
    assert analyzed["usage"]["cache_hit_ratio"] == 0.4
    assert analyzed["tools"]["by_category"] == {"invalid_input": 1}
    assert report["evidence_quality"]["atif_fallback_trials"] == 1
