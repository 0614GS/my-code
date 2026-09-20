"""从 canonical Session 只读导出技术无关的 trajectory DTO。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from my_code.conversation.models import (
    AssistantMessage,
    HumanMessage,
    ReasoningContent,
    TextContent,
    ToolCall,
    ToolResultBatch,
)
from my_code.foundation.json import JsonObject, JsonValue
from my_code.sessions.inspection import SessionInspection, inspect_session


def export_session_trajectory(snapshot: SessionInspection) -> JsonObject:
    """保留内容、调用关联和证据缺口，不推断不存在的请求或结果。"""

    steps: list[JsonValue] = []
    evidence_gaps: list[JsonValue] = []
    completed_requests = [
        request
        for request in snapshot.audit.requests
        if request.manifest.status == "completed"
        and request.manifest.purpose.value == "agent"
    ]
    request_index = 0
    known_calls: set[str] = set()
    resolved_calls: set[str] = set()
    for entry in snapshot.conversation:
        if isinstance(entry, HumanMessage):
            user_step: JsonObject = {
                "kind": "user",
                "timestamp": entry.timestamp,
                "message": entry.content,
                "entry_id": entry.uuid,
            }
            steps.append(user_step)
            continue
        if isinstance(entry, AssistantMessage):
            request = (
                completed_requests[request_index]
                if request_index < len(completed_requests)
                else None
            )
            request_index += 1
            texts: list[str] = []
            reasoning: list[str] = []
            disclosures: list[str] = []
            calls: list[JsonValue] = []
            for block in entry.content:
                if isinstance(block, TextContent):
                    texts.append(block.text)
                elif isinstance(block, ReasoningContent):
                    disclosures.append(block.presentation.disclosure)
                    reasoning.extend(block.presentation.parts)
                elif isinstance(block, ToolCall):
                    known_calls.add(block.id)
                    calls.append(
                        {
                            "call_id": block.id,
                            "name": block.name,
                            "arguments": block.input,
                        }
                    )
            usage = entry.usage
            disclosure_values: list[JsonValue] = list(disclosures)
            agent_step: JsonObject = {
                "kind": "agent",
                "timestamp": entry.timestamp,
                "message": "".join(texts),
                "reasoning": "\n".join(reasoning) or None,
                "reasoning_disclosures": disclosure_values,
                "tool_calls": calls,
                "entry_id": entry.uuid,
                "request_id": request.manifest.request_id if request else None,
                "usage": {
                    "prompt_tokens": usage.total_input_tokens,
                    "completion_tokens": usage.output_tokens,
                    "cache_read_tokens": usage.cache_read_input_tokens,
                    "cache_write_tokens": usage.cache_creation_input_tokens,
                    "provider_reported": usage.provider_reported,
                },
            }
            steps.append(agent_step)
            if request is None:
                evidence_gaps.append(
                    {"kind": "missing_request_audit", "entry_id": entry.uuid}
                )
            continue
        if isinstance(entry, ToolResultBatch):
            results: list[JsonValue] = []
            for result in entry.content:
                resolved_calls.add(result.tool_use_id)
                results.append(
                    {
                        "call_id": result.tool_use_id,
                        "content": result.content,
                        "is_error": result.is_error,
                    }
                )
            steps.append(
                {
                    "kind": "observation",
                    "timestamp": entry.timestamp,
                    "source_assistant_id": entry.source_assistant_id,
                    "entry_id": entry.uuid,
                    "results": results,
                }
            )
    for call_id in sorted(known_calls - resolved_calls):
        evidence_gaps.append({"kind": "unresolved_tool_call", "call_id": call_id})
    for request in snapshot.audit.requests:
        if request.manifest.status != "completed":
            evidence_gaps.append(
                {
                    "kind": "request_not_accepted",
                    "request_id": request.manifest.request_id,
                    "status": request.manifest.status,
                    "error_type": request.manifest.error,
                }
            )
    for invocation in snapshot.invocations:
        if invocation.finished is None:
            evidence_gaps.append(
                {
                    "kind": "incomplete_invocation",
                    "invocation_id": invocation.started.invocation_id,
                }
            )
    if snapshot.audit.legacy_missing:
        evidence_gaps.append({"kind": "legacy_request_audit_missing"})
    return {
        "schema_version": 1,
        "session_id": snapshot.start.session_id,
        "agent": {
            "name": "mycode",
            "model": snapshot.start.model,
            "provider": snapshot.start.provider_id,
            "protocol": snapshot.start.provider_protocol,
        },
        "steps": steps,
        "evidence_gaps": evidence_gaps,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_state_dir", type=Path)
    parser.add_argument("session_id")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        dto = export_session_trajectory(
            inspect_session(args.project_state_dir, args.session_id)
        )
    except (OSError, ValueError) as error:
        parser.exit(2, f"Cannot export Session trajectory: {type(error).__name__}\n")
    payload = json.dumps(dto, ensure_ascii=False, indent=2) + "\n"
    if args.output is None:
        print(payload, end="")
    else:
        args.output.write_text(payload, encoding="utf-8")


if __name__ == "__main__":
    main()


__all__ = ["export_session_trajectory"]
