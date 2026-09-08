"""从执行证据派生 badcase 线索，不推断模型意图或自动修改执行策略。"""

from __future__ import annotations

import hashlib
import json

from my_code.conversation.models import (
    AssistantMessage,
    HumanMessage,
    ToolCall,
    ToolResultBatch,
)
from my_code.foundation.json import JsonObject, JsonValue, to_json_object
from my_code.sessions.inspection import SessionInspection


def build_diagnostic_report(
    snapshot: SessionInspection, *, include_content: bool = False
) -> JsonObject:
    """重复模式只是待检查信号；默认报告只包含定位 ID 和计数。"""

    findings: list[JsonValue] = []
    consumers: dict[str, list[JsonValue]] = {}
    for request in snapshot.audit.requests:
        for source_id in {origin.source_id for origin in request.manifest.origins}:
            if source_id is not None:
                consumers.setdefault(source_id, []).append(request.manifest.request_id)
    calls: dict[str, tuple[ToolCall, str]] = {}
    results: set[str] = set()
    rounds: list[tuple[str, str]] = []
    input_tokens = output_tokens = cache_read = cache_creation = 0
    unreported = 0
    for entry in snapshot.conversation:
        if isinstance(entry, HumanMessage):
            _append_repetitions(rounds, findings)
            rounds = []
        elif isinstance(entry, AssistantMessage):
            input_tokens += entry.usage.input_tokens
            output_tokens += entry.usage.output_tokens
            cache_read += entry.usage.cache_read_input_tokens
            cache_creation += entry.usage.cache_creation_input_tokens
            unreported += not entry.usage.provider_reported
            round_calls = [
                block for block in entry.content if isinstance(block, ToolCall)
            ]
            if round_calls:
                signature = json.dumps(
                    [(call.name, call.input) for call in round_calls],
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                )
                rounds.append(
                    (hashlib.sha256(signature.encode()).hexdigest(), entry.uuid)
                )
            else:
                # 正常文本答复打断连续工具轮次，避免跨完成边界误报。
                _append_repetitions(rounds, findings)
                rounds = []
            for call in round_calls:
                calls[call.id] = (call, entry.uuid)
        elif isinstance(entry, ToolResultBatch):
            for result in entry.content:
                results.add(result.tool_use_id)
                if not result.is_error:
                    continue
                call_info = calls.get(result.tool_use_id)
                finding: JsonObject = {
                    "kind": "tool_error",
                    "tool_call_id": result.tool_use_id,
                    "assistant_id": entry.source_assistant_id,
                    "result_entry_id": entry.uuid,
                    "tool_name": call_info[0].name if call_info else None,
                    "requests_consuming_result": consumers.get(entry.uuid, []),
                }
                if include_content:
                    finding["model_visible_result"] = result.content
                    if call_info:
                        finding["tool_input"] = call_info[0].input
                findings.append(finding)
    _append_repetitions(rounds, findings)
    for call_id, (call, assistant_id) in calls.items():
        if call_id not in results:
            findings.append(
                {
                    "kind": "unresolved_tool_call",
                    "tool_call_id": call_id,
                    "tool_name": call.name,
                    "assistant_id": assistant_id,
                }
            )
    for invocation in snapshot.invocations:
        outcome = invocation.finished.outcome if invocation.finished else "incomplete"
        if outcome != "succeeded":
            findings.append(
                {
                    "kind": "invocation_outcome",
                    "outcome": outcome,
                    "invocation_id": invocation.started.invocation_id,
                    "error_type": invocation.finished.error_type
                    if invocation.finished
                    else None,
                }
            )
    requests: list[JsonValue] = []
    for request in snapshot.audit.requests:
        manifest = request.manifest
        requests.append(
            {
                "request_id": manifest.request_id,
                "number": manifest.request_number,
                "step": manifest.step,
                "attempt": manifest.attempt,
                "purpose": manifest.purpose.value,
                "status": manifest.status,
                "causal_head": manifest.causal_head,
                "error_type": manifest.error,
            }
        )
        if manifest.status != "completed":
            findings.append(
                {
                    "kind": "request_outcome",
                    "request_id": manifest.request_id,
                    "status": manifest.status,
                    "error_type": manifest.error,
                }
            )
    return {
        "version": 1,
        "session_id": snapshot.start.session_id,
        "findings": findings,
        "requests": requests,
        "committed_assistant_usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read,
            "cache_creation_tokens": cache_creation,
            "unreported_messages": unreported,
        },
        "limitations": [
            "Repeated tool rounds are signals, not proof of a loop or its cause.",
            "Usage covers committed assistant messages, not all requests or billing.",
            "Large tool results may be previews; temporary full output can expire.",
            "Inspect a stopped session or a consistent copy, not a live writer.",
            *(
                ["Historical request audit is missing."]
                if snapshot.audit.legacy_missing
                else []
            ),
        ],
    }


def request_evidence(snapshot: SessionInspection, request_id: str) -> JsonObject:
    """显式选择一个请求的原始语义输入，供比较工具错误提示及上下文变化。"""

    for request in snapshot.audit.requests:
        if request.manifest.request_id == request_id:
            return to_json_object(
                {
                    "request_id": request_id,
                    "system_prompt_sections": list(request.system_prompt_sections),
                    "input": list(request.input),
                    "tools": list(request.tools),
                    "budget": request.manifest.budget,
                }
            )
    raise ValueError(f"Unknown request ID: {request_id}")


def _append_repetitions(
    rounds: list[tuple[str, str]], findings: list[JsonValue]
) -> None:
    # 只检查相邻的 1~4 轮模式、至少三次重复，避免把同轮并行调用算成死循环。
    index = 0
    while index < len(rounds):
        for period in range(1, 5):
            pattern = [signature for signature, _ in rounds[index : index + period]]
            if len(pattern) != period:
                continue
            end = index + period
            while (
                end + period <= len(rounds)
                and [signature for signature, _ in rounds[end : end + period]]
                == pattern
            ):
                end += period
            count = (end - index) // period
            if count < 3:
                continue
            findings.append(
                {
                    "kind": "repeated_tool_rounds",
                    "period": period,
                    "repetitions": count,
                    "assistant_ids": [entry_id for _, entry_id in rounds[index:end]],
                }
            )
            index = end
            break
        else:
            index += 1


__all__ = ["build_diagnostic_report", "request_evidence"]
