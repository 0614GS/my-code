"""从 canonical Session 与诊断时间线派生确定性评测指标。"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from typing import Any

from my_code.conversation.models import AssistantMessage, ToolCall, ToolResultBatch
from my_code.foundation.json import JsonObject, to_json_object
from my_code.sessions.diagnostics import build_diagnostic_report
from my_code.sessions.inspection import SessionInspection

RULE_VERSION = 1
_ACTIONABLE_CATEGORIES = {
    "invalid_input",
    "permission_denied",
    "precondition_failed",
    "unknown_tool",
}


def analyze_session(
    snapshot: SessionInspection,
    diagnostic_timeline: dict[str, object] | None = None,
    *,
    include_content: bool = False,
) -> JsonObject:
    """关联会话正文与可选诊断事件；缺失观测只形成证据缺口。"""

    records = _records(diagnostic_timeline)
    permission_by_call: dict[str, dict[str, object]] = {}
    execution_by_call: dict[str, dict[str, object]] = {}
    model_by_request: dict[str, dict[str, object]] = {}
    for record in records:
        event = record.get("event")
        call_id = record.get("tool_call_id")
        if event == "tool.permission.evaluated" and isinstance(call_id, str):
            permission_by_call[call_id] = record
        elif event == "tool.execution.finished" and isinstance(call_id, str):
            execution_by_call[call_id] = record
        request_id = record.get("request_id")
        if not isinstance(request_id, str):
            continue
        fact = model_by_request.setdefault(request_id, {"request_id": request_id})
        if event == "model.request.started":
            fact.update(
                purpose=record.get("purpose"),
                step=record.get("step"),
                attempt=record.get("attempt"),
            )
        elif event == "model.response.received":
            fact.update(
                response_received=True,
                first_event_ms=record.get("first_event_ms"),
                first_text_ms=record.get("first_text_ms"),
            )
        elif event == "model.request.finished":
            fact.update(
                outcome=record.get("outcome"),
                duration_ms=record.get("duration_ms"),
                error_type=record.get("error_type"),
            )

    consumers: dict[str, list[str]] = {}
    request_statuses = Counter[str]()
    for request in snapshot.audit.requests:
        request_statuses[request.manifest.status] += 1
        for origin in request.manifest.origins:
            if origin.source_id is not None:
                consumers.setdefault(origin.source_id, []).append(
                    request.manifest.request_id
                )

    calls: dict[str, dict[str, Any]] = {}
    results: dict[str, tuple[object, str]] = {}
    assistant_index = 0
    usage: dict[str, Any] = {
        "input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "total_input_tokens": 0,
        "output_tokens": 0,
        "provider_reported_messages": 0,
        "unreported_messages": 0,
    }
    for entry in snapshot.conversation:
        if isinstance(entry, AssistantMessage):
            assistant_index += 1
            usage["input_tokens"] += entry.usage.input_tokens
            usage["cache_creation_input_tokens"] += (
                entry.usage.cache_creation_input_tokens
            )
            usage["cache_read_input_tokens"] += entry.usage.cache_read_input_tokens
            usage["total_input_tokens"] += entry.usage.total_input_tokens
            usage["output_tokens"] += entry.usage.output_tokens
            reported_key = (
                "provider_reported_messages"
                if entry.usage.provider_reported
                else "unreported_messages"
            )
            usage[reported_key] += 1
            for block in entry.content:
                if not isinstance(block, ToolCall):
                    continue
                serialized = json.dumps(
                    [block.name, block.input],
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                )
                calls[block.id] = {
                    "tool_call_id": block.id,
                    "tool_name": block.name,
                    "assistant_id": entry.uuid,
                    "assistant_step": assistant_index,
                    "input_sha256": hashlib.sha256(serialized.encode()).hexdigest(),
                    "tool_input": block.input if include_content else None,
                }
        elif isinstance(entry, ToolResultBatch):
            for result in entry.content:
                results[result.tool_use_id] = (result, entry.uuid)

    tool_facts: list[dict[str, Any]] = []
    incidents: list[dict[str, Any]] = []
    for call in calls.values():
        call_id = str(call["tool_call_id"])
        tool_name = str(call["tool_name"])
        permission = permission_by_call.get(call_id)
        execution = execution_by_call.get(call_id)
        pair = results.get(call_id)
        result = pair[0] if pair is not None else None
        result_entry_id = pair[1] if pair is not None else None
        content = getattr(result, "content", None)
        is_error = bool(getattr(result, "is_error", False)) if result else None
        if result is None:
            classification = _classification("unresolved", "call_result_link", "exact")
            status = "unresolved"
        elif not is_error:
            classification = None
            status = "succeeded"
        else:
            classification = classify_tool_failure(
                tool_name,
                content if isinstance(content, str) else "",
                permission_behavior=(
                    str(permission.get("behavior")) if permission else None
                ),
            )
            status = "failed"
        fact = {
            **call,
            "tool_input": call["tool_input"] if include_content else None,
            "result_entry_id": result_entry_id,
            "status": status,
            "is_error": is_error,
            "duration_ms": execution.get("duration_ms") if execution else None,
            "permission": (
                {
                    "behavior": permission.get("behavior"),
                    "reason_kind": permission.get("reason_kind"),
                    "authority": permission.get("authority"),
                    "execution_backend": permission.get("execution_backend"),
                }
                if permission
                else None
            ),
            "classification": classification,
            "requests_consuming_result": consumers.get(str(result_entry_id), []),
            "exact_retry_succeeded": False,
        }
        if include_content and isinstance(content, str):
            fact["result_content"] = content
        tool_facts.append(fact)

    for index, fact in enumerate(tool_facts):
        if fact["status"] != "failed":
            continue
        fingerprint = fact["input_sha256"]
        fact["exact_retry_succeeded"] = any(
            later["input_sha256"] == fingerprint and later["status"] == "succeeded"
            for later in tool_facts[index + 1 :]
        )

    for fact in tool_facts:
        classification = fact.get("classification")
        if not isinstance(classification, dict):
            continue
        incident = {
            "kind": classification["category"],
            "tool_name": fact["tool_name"],
            "tool_call_id": fact["tool_call_id"],
            "assistant_id": fact["assistant_id"],
            "result_entry_id": fact["result_entry_id"],
            "error_signature": classification.get("error_signature"),
            "rule_id": classification["rule_id"],
            "rule_version": classification["rule_version"],
            "classification_source": classification["source"],
            "confidence": classification["confidence"],
            "exact_retry_succeeded": fact["exact_retry_succeeded"],
        }
        if include_content:
            incident["tool_input"] = fact.get("tool_input")
            incident["result_content"] = fact.get("result_content")
        incidents.append(incident)

    diagnostic_report = build_diagnostic_report(snapshot)
    raw_findings = diagnostic_report.get("findings")
    findings = raw_findings if isinstance(raw_findings, list) else []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        if finding.get("kind") not in {
            "repeated_tool_rounds",
            "request_outcome",
            "invocation_outcome",
        }:
            continue
        incidents.append({str(key): value for key, value in finding.items()})

    evidence_gaps = _evidence_gaps(snapshot, diagnostic_timeline, records)
    for gap in evidence_gaps:
        incidents.append({"kind": "evidence_gap", "gap": gap})

    by_tool: list[dict[str, Any]] = []
    for tool_name in sorted({str(item["tool_name"]) for item in tool_facts}):
        selected = [item for item in tool_facts if item["tool_name"] == tool_name]
        resolved = [item for item in selected if item["status"] != "unresolved"]
        failed = [item for item in resolved if item["status"] == "failed"]
        durations = _numeric_values(selected, "duration_ms")
        by_tool.append(
            {
                "tool_name": tool_name,
                "emitted": len(selected),
                "resolved": len(resolved),
                "succeeded": len(resolved) - len(failed),
                "failed": len(failed),
                "unresolved": len(selected) - len(resolved),
                "success_rate": _rate(len(resolved) - len(failed), len(resolved)),
                "duration_ms_p50": _percentile(durations, 0.50),
                "duration_ms_p95": _percentile(durations, 0.95),
            }
        )

    categories = Counter(
        str(item["classification"]["category"])
        for item in tool_facts
        if isinstance(item.get("classification"), dict)
    )
    resolved_count = sum(item["status"] != "unresolved" for item in tool_facts)
    failed_count = sum(item["status"] == "failed" for item in tool_facts)
    permission_evaluations = sum(
        item.get("permission") is not None for item in tool_facts
    )
    permission_denials = sum(
        isinstance(item.get("permission"), dict)
        and item["permission"].get("behavior") == "deny"
        for item in tool_facts
    )
    actionable = sum(categories[category] for category in _ACTIONABLE_CATEGORIES)
    usage["cache_hit_ratio"] = _rate(
        usage["cache_read_input_tokens"], usage["total_input_tokens"]
    )
    model_requests = list(model_by_request.values())
    request_durations = _numeric_values(model_requests, "duration_ms")
    return to_json_object(
        {
            "schema_version": 1,
            "session_id": snapshot.start.session_id,
            "evidence_source": "canonical_session",
            "usage": usage,
            "requests": {
                "count": len(snapshot.audit.requests),
                "by_status": dict(sorted(request_statuses.items())),
                "duration_ms_p50": _percentile(request_durations, 0.50),
                "duration_ms_p95": _percentile(request_durations, 0.95),
            },
            "model_requests": model_requests,
            "tools": {
                "emitted": len(tool_facts),
                "resolved": resolved_count,
                "succeeded": resolved_count - failed_count,
                "failed": failed_count,
                "unresolved": len(tool_facts) - resolved_count,
                "raw_success_rate": _rate(
                    resolved_count - failed_count, resolved_count
                ),
                "resolution_rate": _rate(resolved_count, len(tool_facts)),
                "actionable_call_issues": actionable,
                "actionable_call_issue_rate": _rate(actionable, len(tool_facts)),
                "permission_evaluations": permission_evaluations,
                "permission_denials": permission_denials,
                "permission_denial_rate": _rate(
                    permission_denials, permission_evaluations
                ),
                "by_category": dict(sorted(categories.items())),
                "by_tool": by_tool,
            },
            "tool_calls": tool_facts,
            "incidents": incidents,
            "evidence_quality": {
                "evidence_gaps": evidence_gaps,
                "diagnostic_records": len(records),
                "malformed_diagnostic_records": _integer(
                    diagnostic_timeline, "malformed_records"
                ),
                "dropped_diagnostic_events": max(
                    (
                        _integer(record, "dropped_events")
                        for record in records
                        if _integer(record, "dropped_events") > 0
                    ),
                    default=0,
                ),
            },
        }
    )


def classify_tool_failure(
    tool_name: str,
    content: str,
    *,
    permission_behavior: str | None = None,
) -> JsonObject:
    """使用稳定、版本化规则分类工具失败，不推断模型意图。"""

    first_line = content.splitlines()[0].strip() if content else ""
    lowered = first_line.casefold()
    if permission_behavior == "deny":
        category, rule_id, source, confidence = (
            "permission_denied",
            "permission.behavior.deny",
            "diagnostic_event",
            "exact",
        )
    elif first_line.startswith(("Invalid input:", "Invalid approved input:")):
        category, rule_id, source, confidence = (
            "invalid_input",
            "result.invalid_input_prefix",
            "canonical_result",
            "high",
        )
    elif first_line.startswith("Unknown tool:"):
        category, rule_id, source, confidence = (
            "unknown_tool",
            "result.unknown_tool_prefix",
            "canonical_result",
            "high",
        )
    elif "timed out" in lowered:
        category, rule_id, source, confidence = (
            "timeout",
            "result.timeout_phrase",
            "canonical_result",
            "high",
        )
    elif "aborted" in lowered or "cancelled" in lowered or "canceled" in lowered:
        category, rule_id, source, confidence = (
            "cancelled",
            "result.cancel_phrase",
            "canonical_result",
            "high",
        )
    elif tool_name == "Bash" and re.fullmatch(r"exit_code: [1-9]\d*", first_line):
        category, rule_id, source, confidence = (
            "command_nonzero",
            "bash.nonzero_exit",
            "canonical_result",
            "high",
        )
    elif any(
        phrase in first_line
        for phrase in (
            "Read the entire current file before editing it",
            "File changed since Read; Read it again",
            "old_string was not found",
            "old_string is not unique",
        )
    ):
        category, rule_id, source, confidence = (
            "precondition_failed",
            "result.file_precondition",
            "canonical_result",
            "high",
        )
    elif first_line.startswith("Unexpected "):
        category, rule_id, source, confidence = (
            "unexpected_internal_error",
            "result.unexpected_prefix",
            "canonical_result",
            "high",
        )
    elif first_line.startswith("ToolExecutionError:"):
        category, rule_id, source, confidence = (
            "tool_execution_error",
            "result.tool_execution_prefix",
            "canonical_result",
            "medium",
        )
    else:
        category, rule_id, source, confidence = (
            "unclassified_error",
            "result.fallback",
            "canonical_result",
            "low",
        )
    result = _classification(category, rule_id, confidence, source=source)
    result["error_signature"] = hashlib.sha256(
        f"{tool_name}\0{category}\0{first_line}".encode()
    ).hexdigest()
    return to_json_object(result)


def _classification(
    category: str,
    rule_id: str,
    confidence: str,
    *,
    source: str = "canonical_session",
) -> dict[str, Any]:
    return {
        "category": category,
        "rule_id": rule_id,
        "rule_version": RULE_VERSION,
        "source": source,
        "confidence": confidence,
    }


def _records(timeline: dict[str, object] | None) -> list[dict[str, object]]:
    raw = timeline.get("records") if timeline is not None else None
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _evidence_gaps(
    snapshot: SessionInspection,
    timeline: dict[str, object] | None,
    records: list[dict[str, object]],
) -> list[str]:
    gaps: list[str] = []
    if snapshot.audit.legacy_missing:
        gaps.append("legacy_request_audit_missing")
    if timeline is None:
        gaps.append("diagnostic_timeline_not_loaded")
    else:
        gap = timeline.get("evidence_gap")
        if isinstance(gap, str):
            gaps.append(gap)
        if _integer(timeline, "malformed_records") > 0:
            gaps.append("diagnostic_records_malformed")
    if records and any(_integer(record, "dropped_events") > 0 for record in records):
        gaps.append("diagnostic_events_dropped")
    return list(dict.fromkeys(gaps))


def _integer(value: dict[str, object] | None, key: str) -> int:
    raw = value.get(key) if value is not None else None
    return raw if isinstance(raw, int) else 0


def _numeric_values(items: list[dict[str, Any]], key: str) -> list[float]:
    values: list[float] = []
    for item in items:
        value = item.get(key)
        if isinstance(value, (int, float)):
            values.append(float(value))
    return values


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * percentile)]


__all__ = ["RULE_VERSION", "analyze_session", "classify_tool_failure"]
