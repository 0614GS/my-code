"""联合 Harbor trial result 与 my-code 原生证据生成只读 badcase 汇总。"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from pathlib import Path
from typing import Any

from my_code.sessions.diagnostics import build_diagnostic_report
from my_code.sessions.inspection import inspect_session


def _load(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _duration_ms(raw: object) -> float | None:
    if not isinstance(raw, dict):
        return None
    started = raw.get("started_at")
    finished = raw.get("finished_at")
    if not isinstance(started, str) or not isinstance(finished, str):
        return None
    from datetime import datetime

    try:
        return (
            datetime.fromisoformat(finished) - datetime.fromisoformat(started)
        ).total_seconds() * 1000
    except ValueError:
        return None


def _native_findings(agent_dir: Path, session_id: object) -> list[dict[str, Any]]:
    if not isinstance(session_id, str):
        return []
    for session_log in (agent_dir / "mycode" / "projects").glob(
        f"*/{session_id}.jsonl"
    ):
        try:
            report = build_diagnostic_report(
                inspect_session(session_log.parent, session_id)
            )
        except (OSError, ValueError):
            return [{"kind": "damaged_session"}]
        findings = report.get("findings")
        return (
            [item for item in findings if isinstance(item, dict)]
            if isinstance(findings, list)
            else []
        )
    return [{"kind": "missing_session"}]


def collect_trials(job_dir: Path) -> list[dict[str, Any]]:
    trials: list[dict[str, Any]] = []
    result_paths = sorted(
        {
            *job_dir.rglob("result.json"),
            *job_dir.rglob("results.json"),
        }
    )
    for result_path in result_paths:
        if result_path.parent.name in {"agent", "verifier"}:
            continue
        harbor = _load(result_path)
        if harbor is None or "task_name" not in harbor:
            continue
        agent_dir = result_path.parent / "agent"
        native = _load(agent_dir / "result.json") or {}
        raw_usage = native.get("usage")
        usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
        verifier = harbor.get("verifier_result")
        rewards = verifier.get("rewards") if isinstance(verifier, dict) else None
        reward_values = (
            [float(value) for value in rewards.values()]
            if isinstance(rewards, dict)
            else []
        )
        findings = _native_findings(agent_dir, native.get("session_id"))
        kinds = [str(item.get("kind")) for item in findings]
        raw_error = native.get("error")
        error: dict[str, Any] = raw_error if isinstance(raw_error, dict) else {}
        exception = harbor.get("exception_info")
        trials.append(
            {
                "trial": harbor.get("trial_name") or result_path.parent.name,
                "task": harbor.get("task_name"),
                "reward": statistics.mean(reward_values) if reward_values else None,
                "passed": bool(reward_values)
                and all(value >= 1 for value in reward_values),
                "agent_outcome": native.get("outcome") or "incomplete",
                "exception_type": (
                    exception.get("exception_type")
                    if isinstance(exception, dict)
                    else error.get("code")
                ),
                "incomplete": not bool(native),
                "input_tokens": usage.get("total_input_tokens"),
                "cache_read_tokens": usage.get("cache_read_input_tokens"),
                "cache_write_tokens": usage.get("cache_creation_input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "cache_hit_ratio": (
                    float(usage.get("cache_read_input_tokens") or 0)
                    / float(usage.get("total_input_tokens") or 1)
                ),
                "steps": native.get("completed_steps"),
                "duration_ms": native.get("duration_ms")
                or _duration_ms(harbor.get("agent_execution")),
                "max_steps": native.get("outcome") == "max_steps",
                "timeout": native.get("outcome") == "timed_out",
                "tool_errors": kinds.count("tool_error"),
                "permission_denials": kinds.count("permission_denied"),
                "repeated_tool_patterns": kinds.count("repeated_tool_rounds"),
                "evidence_findings": findings,
            }
        )
    return trials


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = round((len(ordered) - 1) * percentile)
    return ordered[index]


def summarize(trials: list[dict[str, Any]]) -> dict[str, Any]:
    tokens = [
        float(item["input_tokens"] or 0) + float(item["output_tokens"] or 0)
        for item in trials
    ]
    durations = [
        float(item["duration_ms"]) for item in trials if item["duration_ms"] is not None
    ]
    return {
        "trial_count": len(trials),
        "pass_rate": sum(bool(item["passed"]) for item in trials) / len(trials)
        if trials
        else None,
        "agent_failure_rate": sum(
            item["agent_outcome"] != "succeeded" for item in trials
        )
        / len(trials)
        if trials
        else None,
        "token_p50": _percentile(tokens, 0.5),
        "token_p95": _percentile(tokens, 0.95),
        "duration_ms_p50": _percentile(durations, 0.5),
        "duration_ms_p95": _percentile(durations, 0.95),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_dir", type=Path)
    parser.add_argument("--format", choices=("json", "csv"), default="json")
    args = parser.parse_args()
    trials = collect_trials(args.job_dir)
    if args.format == "json":
        print(json.dumps({"summary": summarize(trials), "trials": trials}, indent=2))
        return
    fieldnames = (
        [key for key in trials[0] if key != "evidence_findings"] if trials else []
    )
    writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(
        {key: value for key, value in item.items() if key in fieldnames}
        for item in trials
    )


if __name__ == "__main__":
    main()
