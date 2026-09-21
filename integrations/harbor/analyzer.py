"""只读聚合 Harbor job 与 my-code 原生执行证据。"""

from __future__ import annotations

import csv
import io
import json
import statistics
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from my_code.observability.diagnostic_log import read_diagnostic_timeline
from my_code.sessions.analysis import analyze_session, classify_tool_failure
from my_code.sessions.inspection import inspect_session

REPORT_SCHEMA_VERSION = 1


def analyze_harbor_job(
    job_dir: Path, *, include_content: bool = False
) -> dict[str, Any]:
    """分析一个已停止写入的 Harbor job，不修改任何 trial 证据。"""

    job_dir = job_dir.resolve()
    trials = [
        _analyze_trial(job_dir, path, include_content=include_content)
        for path in sorted(path for path in job_dir.iterdir() if path.is_dir())
        if _is_trial(path)
    ]
    if not trials:
        raise ValueError(f"No Harbor trials found in {job_dir}")
    job_result = _load(job_dir / "result.json") or {}
    incidents: list[dict[str, Any]] = []
    for trial in trials:
        for raw in trial["incidents"]:
            incident = dict(raw)
            incident.setdefault("trial", trial["trial"])
            incident.setdefault("task", trial["task"])
            incidents.append(incident)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "job": {
            "id": job_result.get("id"),
            "directory": str(job_dir),
            "started_at": job_result.get("started_at"),
            "finished_at": job_result.get("finished_at"),
            "dataset": _single_value(
                str(trial["dataset"])
                for trial in trials
                if trial.get("dataset") is not None
            ),
        },
        "summary": _summarize(trials),
        "aggregates": {
            "by_tool": _aggregate_tools(trials),
            "by_error_category": _aggregate_categories(trials),
            "by_task": _aggregate_tasks(trials),
        },
        "evidence_quality": _aggregate_evidence_quality(trials),
        "trials": trials,
        "incidents": incidents,
    }


def write_report(report: dict[str, Any], output_dir: Path) -> tuple[Path, ...]:
    """原子写入固定报告文件，不清理目录中的其他内容。"""

    output_dir.mkdir(parents=True, exist_ok=True)
    paths = (
        output_dir / "report.json",
        output_dir / "trials.csv",
        output_dir / "tools.csv",
        output_dir / "incidents.csv",
    )
    _atomic_write(paths[0], json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    _atomic_write(paths[1], _trials_csv(report.get("trials", [])))
    aggregates = report.get("aggregates")
    by_tool = aggregates.get("by_tool", []) if isinstance(aggregates, dict) else []
    _atomic_write(paths[2], _dict_rows_csv(by_tool))
    _atomic_write(paths[3], _incidents_csv(report.get("incidents", [])))
    return paths


def render_summary(report: dict[str, Any], paths: tuple[Path, ...] = ()) -> str:
    """生成适合批量评测结束后阅读的短摘要。"""

    summary = report.get("summary")
    data: dict[str, Any] = summary if isinstance(summary, dict) else {}
    evaluation = data.get("evaluation")
    usage = data.get("usage")
    tools = data.get("tools")
    evidence = report.get("evidence_quality")
    evaluation = evaluation if isinstance(evaluation, dict) else {}
    usage = usage if isinstance(usage, dict) else {}
    tools = tools if isinstance(tools, dict) else {}
    evidence = evidence if isinstance(evidence, dict) else {}
    lines = [
        (
            f"Analyzed {data.get('trial_count', 0)} trial(s): "
            f"pass_rate={_format_rate(evaluation.get('pass_rate'))}, "
            f"agent_failure_rate={_format_rate(evaluation.get('agent_failure_rate'))}"
        ),
        (
            f"Tokens: total_input={usage.get('total_input_tokens', 0)}, "
            f"cache_read={usage.get('cache_read_input_tokens', 0)}, "
            f"cache_hit={_format_rate(usage.get('cache_hit_ratio'))}"
        ),
        (
            f"Tools: emitted={tools.get('emitted', 0)}, "
            f"success={_format_rate(tools.get('raw_success_rate'))}, "
            f"actionable_issues={tools.get('actionable_call_issues', 0)}"
        ),
        (
            "Evidence: canonical_sessions="
            f"{evidence.get('canonical_session_trials', 0)}"
            f"/{data.get('trial_count', 0)}, gaps={evidence.get('trials_with_gaps', 0)}"
        ),
    ]
    if paths:
        lines.append(f"Reports written to {paths[0].parent}")
    return "\n".join(lines)


def trials_csv(report: dict[str, Any]) -> str:
    """返回兼容旧 ``--format csv`` 的 trial 表格。"""

    return _trials_csv(report.get("trials", []))


def _analyze_trial(
    job_dir: Path, trial_dir: Path, *, include_content: bool
) -> dict[str, Any]:
    harbor = _load(trial_dir / "result.json") or {}
    agent_dir = trial_dir / "agent"
    native = _load(agent_dir / "result.json") or {}
    session_id = native.get("session_id")
    session_log = _find_session_log(agent_dir, session_id)
    analysis: dict[str, Any] | None = None
    trial_incidents: list[dict[str, Any]] = []
    session_error: str | None = None
    if session_log is not None and isinstance(session_id, str):
        try:
            snapshot = inspect_session(session_log.parent, session_id)
            timeline = read_diagnostic_timeline(session_log.parent, session_id)
            analysis = analyze_session(
                snapshot, timeline, include_content=include_content
            )
        except (OSError, UnicodeError, ValueError) as caught:
            session_error = type(caught).__name__
    if analysis is None:
        analysis = _analyze_atif(
            agent_dir / "trajectory.json", include_content=include_content
        )
        gap = (
            "canonical_session_damaged"
            if session_error
            else "canonical_session_missing"
        )
        trial_incidents.append(
            {
                "kind": "evidence_gap",
                "gap": gap,
                "error_type": session_error,
            }
        )
    if analysis is None:
        analysis = _empty_analysis()

    native_usage = _native_usage(native)
    session_usage = analysis.get("usage")
    if isinstance(session_usage, dict) and native_usage:
        differences = {
            key: {
                "canonical": session_usage.get(key),
                "terminal": native_usage.get(key),
            }
            for key in (
                "input_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
                "total_input_tokens",
                "output_tokens",
            )
            if session_usage.get(key) != native_usage.get(key)
        }
        if differences:
            trial_incidents.append(
                {
                    "kind": "usage_mismatch",
                    "differences": differences,
                    "preferred_source": analysis.get("evidence_source"),
                }
            )
    usage = session_usage if isinstance(session_usage, dict) else native_usage
    usage = dict(usage or {})
    total_input = _number(usage.get("total_input_tokens"))
    cache_read = _number(usage.get("cache_read_input_tokens"))
    usage["cache_hit_ratio"] = cache_read / total_input if total_input else None

    verifier = harbor.get("verifier_result")
    rewards = verifier.get("rewards") if isinstance(verifier, dict) else None
    reward_values = (
        [float(value) for value in rewards.values() if isinstance(value, (int, float))]
        if isinstance(rewards, dict)
        else []
    )
    exception = harbor.get("exception_info")
    raw_error = native.get("error")
    error: dict[str, Any] = raw_error if isinstance(raw_error, dict) else {}
    exception_type = (
        exception.get("exception_type")
        if isinstance(exception, dict)
        else error.get("code")
    )
    relative_session = (
        str(session_log.relative_to(job_dir)) if session_log is not None else None
    )
    analysis_incidents = analysis.get("incidents")
    if isinstance(analysis_incidents, list):
        for raw in analysis_incidents:
            if not isinstance(raw, dict):
                continue
            incident = dict(raw)
            incident.setdefault(
                "evidence_refs",
                [
                    {
                        "source": analysis.get("evidence_source"),
                        "session_id": session_id,
                        "session_log": relative_session,
                        "tool_call_id": incident.get("tool_call_id"),
                        "result_entry_id": incident.get("result_entry_id"),
                    }
                ],
            )
            trial_incidents.append(incident)
    timings = {
        phase: _duration_ms(harbor.get(key))
        for phase, key in (
            ("environment_setup_ms", "environment_setup"),
            ("agent_setup_ms", "agent_setup"),
            ("agent_execution_ms", "agent_execution"),
            ("verifier_ms", "verifier"),
        )
    }
    timings["total_ms"] = _duration_between(
        harbor.get("started_at"), harbor.get("finished_at")
    )
    task_checksum = harbor.get("task_checksum")
    task = harbor.get("task_name")
    return {
        "trial": harbor.get("trial_name") or trial_dir.name,
        "task": task,
        "task_key": f"{task}:{task_checksum}" if task_checksum else str(task),
        "dataset": harbor.get("source"),
        "reward": statistics.mean(reward_values) if reward_values else None,
        "passed": bool(reward_values) and all(value >= 1 for value in reward_values),
        "agent_outcome": native.get("outcome") or "incomplete",
        "exception_type": exception_type,
        "incomplete": not bool(native),
        "steps": native.get("completed_steps"),
        "usage": usage,
        "timings": timings,
        "requests": analysis.get("requests"),
        "model_requests": analysis.get("model_requests"),
        "tools": analysis.get("tools"),
        "tool_calls": analysis.get("tool_calls"),
        "evidence_source": analysis.get("evidence_source"),
        "evidence_quality": analysis.get("evidence_quality"),
        "session_id": session_id,
        "run_id": native.get("run_id"),
        "invocation_id": native.get("invocation_id"),
        "incidents": trial_incidents,
    }


def _analyze_atif(path: Path, *, include_content: bool) -> dict[str, Any] | None:
    dto = _load(path)
    if dto is None or dto.get("schema_version") != "ATIF-v1.7":
        return None
    calls: dict[str, dict[str, Any]] = {}
    usage = {
        "input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "total_input_tokens": 0,
        "output_tokens": 0,
    }
    for step_index, step in enumerate(dto.get("steps") or [], start=1):
        if not isinstance(step, dict) or step.get("source") != "agent":
            continue
        metrics = step.get("metrics")
        if isinstance(metrics, dict):
            prompt = int(metrics.get("prompt_tokens") or 0)
            cached = int(metrics.get("cached_tokens") or 0)
            extra = metrics.get("extra")
            cache_write = (
                int(extra.get("cache_write_tokens") or 0)
                if isinstance(extra, dict)
                else 0
            )
            usage["total_input_tokens"] += prompt
            usage["cache_read_input_tokens"] += cached
            usage["cache_creation_input_tokens"] += cache_write
            usage["input_tokens"] += max(0, prompt - cached - cache_write)
            usage["output_tokens"] += int(metrics.get("completion_tokens") or 0)
        for raw in step.get("tool_calls") or []:
            if not isinstance(raw, dict) or not isinstance(
                raw.get("tool_call_id"), str
            ):
                continue
            serialized = json.dumps(
                [raw.get("function_name"), raw.get("arguments") or {}],
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
            calls[raw["tool_call_id"]] = {
                "tool_call_id": raw["tool_call_id"],
                "tool_name": raw.get("function_name"),
                "assistant_step": step_index,
                "input_sha256": _sha256(serialized),
                "tool_input": raw.get("arguments") if include_content else None,
                "status": "unresolved",
                "is_error": None,
                "classification": None,
                "duration_ms": None,
                "permission": None,
                "exact_retry_succeeded": False,
            }
        observation = step.get("observation")
        if not isinstance(observation, dict):
            continue
        for raw in observation.get("results") or []:
            if not isinstance(raw, dict):
                continue
            call = calls.get(str(raw.get("source_call_id")))
            if call is None:
                continue
            extra = raw.get("extra")
            is_error = bool(extra.get("is_error")) if isinstance(extra, dict) else False
            content = str(raw.get("content") or "")
            call["is_error"] = is_error
            call["status"] = "failed" if is_error else "succeeded"
            if is_error:
                call["classification"] = classify_tool_failure(
                    str(call["tool_name"]), content
                )
            if include_content:
                call["result_content"] = content
    facts = list(calls.values())
    return _analysis_from_facts(dto.get("session_id"), usage, facts)


def _analysis_from_facts(
    session_id: object, usage: dict[str, Any], facts: list[dict[str, Any]]
) -> dict[str, Any]:
    categories = Counter(
        str(fact["classification"]["category"])
        for fact in facts
        if isinstance(fact.get("classification"), dict)
    )
    resolved = sum(fact["status"] != "unresolved" for fact in facts)
    failed = sum(fact["status"] == "failed" for fact in facts)
    by_tool = []
    for name in sorted({str(fact["tool_name"]) for fact in facts}):
        selected = [fact for fact in facts if str(fact["tool_name"]) == name]
        tool_resolved = sum(fact["status"] != "unresolved" for fact in selected)
        tool_failed = sum(fact["status"] == "failed" for fact in selected)
        by_tool.append(
            {
                "tool_name": name,
                "emitted": len(selected),
                "resolved": tool_resolved,
                "succeeded": tool_resolved - tool_failed,
                "failed": tool_failed,
                "unresolved": len(selected) - tool_resolved,
                "success_rate": _rate(tool_resolved - tool_failed, tool_resolved),
                "duration_ms_p50": None,
                "duration_ms_p95": None,
            }
        )
    incidents = []
    for fact in facts:
        classification = fact.get("classification")
        if not isinstance(classification, dict):
            if fact["status"] == "unresolved":
                incidents.append(
                    {"kind": "unresolved", "tool_call_id": fact["tool_call_id"]}
                )
            continue
        incidents.append(
            {
                "kind": classification.get("category"),
                "tool_call_id": fact["tool_call_id"],
                "tool_name": fact["tool_name"],
                "rule_id": classification.get("rule_id"),
                "rule_version": classification.get("rule_version"),
                "classification_source": classification.get("source"),
                "confidence": classification.get("confidence"),
                "error_signature": classification.get("error_signature"),
            }
        )
    usage["cache_hit_ratio"] = _rate(
        int(usage["cache_read_input_tokens"]), int(usage["total_input_tokens"])
    )
    return {
        "schema_version": 1,
        "session_id": session_id,
        "evidence_source": "atif",
        "usage": usage,
        "requests": None,
        "model_requests": [],
        "tools": {
            "emitted": len(facts),
            "resolved": resolved,
            "succeeded": resolved - failed,
            "failed": failed,
            "unresolved": len(facts) - resolved,
            "raw_success_rate": _rate(resolved - failed, resolved),
            "resolution_rate": _rate(resolved, len(facts)),
            "actionable_call_issues": sum(
                categories[key]
                for key in (
                    "invalid_input",
                    "permission_denied",
                    "precondition_failed",
                    "unknown_tool",
                )
            ),
            "actionable_call_issue_rate": _rate(
                sum(
                    categories[key]
                    for key in (
                        "invalid_input",
                        "permission_denied",
                        "precondition_failed",
                        "unknown_tool",
                    )
                ),
                len(facts),
            ),
            "permission_evaluations": 0,
            "permission_denials": 0,
            "permission_denial_rate": None,
            "by_category": dict(sorted(categories.items())),
            "by_tool": by_tool,
        },
        "tool_calls": facts,
        "incidents": incidents,
        "evidence_quality": {
            "evidence_gaps": ["canonical_session_missing", "diagnostics_unavailable"],
            "diagnostic_records": 0,
            "malformed_diagnostic_records": 0,
            "dropped_diagnostic_events": 0,
        },
    }


def _empty_analysis() -> dict[str, Any]:
    return {
        "evidence_source": "none",
        "usage": None,
        "requests": None,
        "model_requests": [],
        "tools": None,
        "tool_calls": [],
        "incidents": [],
        "evidence_quality": {
            "evidence_gaps": ["all_agent_evidence_missing"],
            "diagnostic_records": 0,
            "malformed_diagnostic_records": 0,
            "dropped_diagnostic_events": 0,
        },
    }


def _summarize(trials: list[dict[str, Any]]) -> dict[str, Any]:
    input_tokens = cache_create = cache_read = output_tokens = 0
    reported_messages = unreported_messages = 0
    token_totals: list[float] = []
    for trial in trials:
        usage = trial.get("usage")
        if not isinstance(usage, dict):
            continue
        input_tokens += int(usage.get("input_tokens") or 0)
        cache_create += int(usage.get("cache_creation_input_tokens") or 0)
        cache_read += int(usage.get("cache_read_input_tokens") or 0)
        output_tokens += int(usage.get("output_tokens") or 0)
        reported_messages += int(usage.get("provider_reported_messages") or 0)
        unreported_messages += int(usage.get("unreported_messages") or 0)
        token_totals.append(
            float(usage.get("total_input_tokens") or 0)
            + float(usage.get("output_tokens") or 0)
        )
    total_input = input_tokens + cache_create + cache_read
    tool_totals = Counter[str]()
    for trial in trials:
        tools = trial.get("tools")
        if not isinstance(tools, dict):
            continue
        for key in (
            "emitted",
            "resolved",
            "succeeded",
            "failed",
            "unresolved",
            "actionable_call_issues",
            "permission_evaluations",
            "permission_denials",
        ):
            tool_totals[key] += int(tools.get(key) or 0)
    timings: dict[str, dict[str, float | None]] = {}
    for key in (
        "environment_setup_ms",
        "agent_setup_ms",
        "agent_execution_ms",
        "verifier_ms",
        "total_ms",
    ):
        values = [
            float(trial["timings"][key])
            for trial in trials
            if isinstance(trial.get("timings"), dict)
            and isinstance(trial["timings"].get(key), (int, float))
        ]
        timings[key] = {
            "p50": _percentile(values, 0.50),
            "p95": _percentile(values, 0.95),
        }
    request_statuses = Counter[str]()
    request_durations: list[float] = []
    first_event_values: list[float] = []
    first_text_values: list[float] = []
    for trial in trials:
        requests = trial.get("requests")
        raw_statuses = requests.get("by_status") if isinstance(requests, dict) else None
        if isinstance(raw_statuses, dict):
            for key, value in raw_statuses.items():
                request_statuses[str(key)] += int(value)
        model_requests = trial.get("model_requests")
        if not isinstance(model_requests, list):
            continue
        for request in model_requests:
            if not isinstance(request, dict):
                continue
            for values, key in (
                (request_durations, "duration_ms"),
                (first_event_values, "first_event_ms"),
                (first_text_values, "first_text_ms"),
            ):
                value = request.get(key)
                if isinstance(value, (int, float)):
                    values.append(float(value))
    return {
        "trial_count": len(trials),
        "task_count": len({str(trial["task_key"]) for trial in trials}),
        "evaluation": {
            "passed_trials": sum(bool(trial["passed"]) for trial in trials),
            "pass_rate": _rate(
                sum(bool(trial["passed"]) for trial in trials), len(trials)
            ),
            "agent_failures": sum(
                trial["agent_outcome"] != "succeeded" for trial in trials
            ),
            "agent_failure_rate": _rate(
                sum(trial["agent_outcome"] != "succeeded" for trial in trials),
                len(trials),
            ),
            "incomplete_trials": sum(bool(trial["incomplete"]) for trial in trials),
        },
        "usage": {
            "input_tokens": input_tokens,
            "cache_creation_input_tokens": cache_create,
            "cache_read_input_tokens": cache_read,
            "total_input_tokens": total_input,
            "output_tokens": output_tokens,
            "cache_hit_ratio": _rate(cache_read, total_input),
            "provider_reported_message_rate": _rate(
                reported_messages, reported_messages + unreported_messages
            ),
            "token_p50": _percentile(token_totals, 0.50),
            "token_p95": _percentile(token_totals, 0.95),
        },
        "model_requests": {
            "count": sum(request_statuses.values()),
            "by_status": dict(sorted(request_statuses.items())),
            "duration_ms_p50": _percentile(request_durations, 0.50),
            "duration_ms_p95": _percentile(request_durations, 0.95),
            "first_event_ms_p50": _percentile(first_event_values, 0.50),
            "first_event_ms_p95": _percentile(first_event_values, 0.95),
            "first_text_ms_p50": _percentile(first_text_values, 0.50),
            "first_text_ms_p95": _percentile(first_text_values, 0.95),
        },
        "tools": {
            **dict(tool_totals),
            "raw_success_rate": _rate(
                tool_totals["succeeded"], tool_totals["resolved"]
            ),
            "resolution_rate": _rate(tool_totals["resolved"], tool_totals["emitted"]),
            "actionable_call_issue_rate": _rate(
                tool_totals["actionable_call_issues"], tool_totals["emitted"]
            ),
            "permission_denial_rate": _rate(
                tool_totals["permission_denials"],
                tool_totals["permission_evaluations"],
            ),
        },
        "timings": timings,
    }


def _aggregate_tools(trials: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, Counter[str]] = defaultdict(Counter)
    durations: dict[str, list[float]] = defaultdict(list)
    for trial in trials:
        tools = trial.get("tools")
        if isinstance(tools, dict):
            for item in tools.get("by_tool") or []:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("tool_name"))
                for key in ("emitted", "resolved", "succeeded", "failed", "unresolved"):
                    grouped[name][key] += int(item.get(key) or 0)
        for fact in trial.get("tool_calls") or []:
            if isinstance(fact, dict) and isinstance(
                fact.get("duration_ms"), (int, float)
            ):
                durations[str(fact.get("tool_name"))].append(float(fact["duration_ms"]))
    return [
        {
            "tool_name": name,
            **dict(counts),
            "success_rate": _rate(counts["succeeded"], counts["resolved"]),
            "duration_ms_p50": _percentile(durations[name], 0.50),
            "duration_ms_p95": _percentile(durations[name], 0.95),
        }
        for name, counts in sorted(grouped.items())
    ]


def _aggregate_categories(trials: list[dict[str, Any]]) -> dict[str, int]:
    categories = Counter[str]()
    for trial in trials:
        tools = trial.get("tools")
        raw = tools.get("by_category") if isinstance(tools, dict) else None
        if isinstance(raw, dict):
            for key, value in raw.items():
                categories[str(key)] += int(value)
    return dict(sorted(categories.items()))


def _aggregate_tasks(trials: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trial in trials:
        grouped[str(trial["task_key"])].append(trial)
    return [
        {
            "task_key": key,
            "task": items[0]["task"],
            "attempts": len(items),
            "passed_attempts": sum(bool(item["passed"]) for item in items),
            "pass_rate": _rate(sum(bool(item["passed"]) for item in items), len(items)),
            "agent_failures": sum(
                item["agent_outcome"] != "succeeded" for item in items
            ),
            "agent_failure_rate": _rate(
                sum(item["agent_outcome"] != "succeeded" for item in items),
                len(items),
            ),
        }
        for key, items in sorted(grouped.items())
    ]


def _aggregate_evidence_quality(trials: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "canonical_session_trials": sum(
            trial.get("evidence_source") == "canonical_session" for trial in trials
        ),
        "atif_fallback_trials": sum(
            trial.get("evidence_source") == "atif" for trial in trials
        ),
        "trials_without_agent_evidence": sum(
            trial.get("evidence_source") == "none" for trial in trials
        ),
        "trials_with_gaps": sum(
            bool((trial.get("evidence_quality") or {}).get("evidence_gaps"))
            if isinstance(trial.get("evidence_quality"), dict)
            else True
            for trial in trials
        ),
        "usage_mismatch_trials": sum(
            any(
                isinstance(item, dict) and item.get("kind") == "usage_mismatch"
                for item in trial["incidents"]
            )
            for trial in trials
        ),
        "malformed_diagnostic_records": sum(
            int(trial["evidence_quality"].get("malformed_diagnostic_records") or 0)
            for trial in trials
            if isinstance(trial.get("evidence_quality"), dict)
        ),
        "dropped_diagnostic_events": sum(
            int(trial["evidence_quality"].get("dropped_diagnostic_events") or 0)
            for trial in trials
            if isinstance(trial.get("evidence_quality"), dict)
        ),
    }


def _trials_csv(raw_trials: object) -> str:
    trials = raw_trials if isinstance(raw_trials, list) else []
    rows = []
    for trial in trials:
        if not isinstance(trial, dict):
            continue
        raw_usage = trial.get("usage")
        raw_tools = trial.get("tools")
        raw_timings = trial.get("timings")
        usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
        tools: dict[str, Any] = raw_tools if isinstance(raw_tools, dict) else {}
        timings: dict[str, Any] = raw_timings if isinstance(raw_timings, dict) else {}
        rows.append(
            {
                "trial": trial.get("trial"),
                "task": trial.get("task"),
                "reward": trial.get("reward"),
                "passed": trial.get("passed"),
                "agent_outcome": trial.get("agent_outcome"),
                "exception_type": trial.get("exception_type"),
                "evidence_source": trial.get("evidence_source"),
                "steps": trial.get("steps"),
                "total_input_tokens": usage.get("total_input_tokens"),
                "cache_read_tokens": usage.get("cache_read_input_tokens"),
                "cache_write_tokens": usage.get("cache_creation_input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "cache_hit_ratio": usage.get("cache_hit_ratio"),
                "tool_calls": tools.get("emitted"),
                "tool_success_rate": tools.get("raw_success_rate"),
                "actionable_call_issues": tools.get("actionable_call_issues"),
                "permission_denials": tools.get("permission_denials"),
                "duration_ms": timings.get("total_ms"),
            }
        )
    return _dict_rows_csv(rows)


def _incidents_csv(raw_incidents: object) -> str:
    incidents = raw_incidents if isinstance(raw_incidents, list) else []
    rows = []
    for item in incidents:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "trial": item.get("trial"),
                "task": item.get("task"),
                "kind": item.get("kind"),
                "tool_name": item.get("tool_name"),
                "tool_call_id": item.get("tool_call_id"),
                "rule_id": item.get("rule_id"),
                "rule_version": item.get("rule_version"),
                "classification_source": item.get("classification_source"),
                "confidence": item.get("confidence"),
                "error_signature": item.get("error_signature"),
                "gap": item.get("gap"),
            }
        )
    return _dict_rows_csv(rows)


def _dict_rows_csv(raw_rows: object) -> str:
    rows = (
        [item for item in raw_rows if isinstance(item, dict)]
        if isinstance(raw_rows, list)
        else []
    )
    if not rows:
        return ""
    fields = list(rows[0])
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def _is_trial(path: Path) -> bool:
    value = _load(path / "result.json")
    return value is not None and "task_name" in value


def _find_session_log(agent_dir: Path, session_id: object) -> Path | None:
    if not isinstance(session_id, str):
        return None
    return next(
        iter((agent_dir / "mycode" / "projects").glob(f"*/{session_id}.jsonl")),
        None,
    )


def _native_usage(native: dict[str, Any]) -> dict[str, Any]:
    raw = native.get("usage")
    if not isinstance(raw, dict):
        return {}
    return {
        "input_tokens": raw.get("input_tokens"),
        "cache_creation_input_tokens": raw.get("cache_creation_input_tokens"),
        "cache_read_input_tokens": raw.get("cache_read_input_tokens"),
        "total_input_tokens": raw.get("total_input_tokens"),
        "output_tokens": raw.get("output_tokens"),
        "provider_reported": raw.get("provider_reported"),
    }


def _load(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _duration_ms(raw: object) -> float | None:
    if not isinstance(raw, dict):
        return None
    return _duration_between(raw.get("started_at"), raw.get("finished_at"))


def _duration_between(started: object, finished: object) -> float | None:
    if not isinstance(started, str) or not isinstance(finished, str):
        return None
    try:
        return (
            datetime.fromisoformat(finished) - datetime.fromisoformat(started)
        ).total_seconds() * 1000
    except ValueError:
        return None


def _atomic_write(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _sha256(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()


def _number(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


def _rate(numerator: int | float, denominator: int | float) -> float | None:
    return numerator / denominator if denominator else None


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * percentile)]


def _single_value(values: Any) -> str | None:
    unique = sorted(set(values))
    return unique[0] if len(unique) == 1 else None


def _format_rate(value: object) -> str:
    return f"{float(value):.1%}" if isinstance(value, (int, float)) else "n/a"


__all__ = [
    "REPORT_SCHEMA_VERSION",
    "analyze_harbor_job",
    "render_summary",
    "trials_csv",
    "write_report",
]
