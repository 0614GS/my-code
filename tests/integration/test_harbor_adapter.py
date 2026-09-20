import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

pytest.importorskip("harbor")

from harbor.models.agent.context import AgentContext

_AGENT_PATH = Path(__file__).parents[2] / "integrations" / "harbor" / "agent.py"
_SPEC = importlib.util.spec_from_file_location("mycode_harbor_agent", _AGENT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
MyCodeAgent = _MODULE.MyCodeAgent
MyCodeConfigurationError = _MODULE.MyCodeConfigurationError
MyCodeIncompleteError = _MODULE.MyCodeIncompleteError
MyCodeMaxStepsError = _MODULE.MyCodeMaxStepsError
_trajectory_to_atif = _MODULE._trajectory_to_atif
load_artifact_manifest = _MODULE.load_artifact_manifest


def _artifacts(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    wheel = tmp_path / "my_code-0.1.0-py3-none-any.whl"
    constraints = tmp_path / "constraints.txt"
    uv_binary = tmp_path / "uv"
    wheel.write_bytes(b"wheel")
    constraints.write_text("anthropic==0.121.0\n", encoding="utf-8")
    uv_binary.write_bytes(b"uv-binary")

    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": "0.1.0",
                "wheel": wheel.name,
                "wheel_sha256": digest(wheel),
                "constraints_sha256": digest(constraints),
                "lock_sha256": "lock-digest",
                "python_version": "3.12",
                "uv": uv_binary.name,
                "uv_sha256": digest(uv_binary),
                "uv_version": "uv 0.11.7",
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


def _agent(tmp_path: Path) -> Any:
    artifacts = _artifacts(tmp_path / "artifacts")
    logs = tmp_path / "logs"
    logs.mkdir()
    return MyCodeAgent(
        logs,
        model_name="claude-test",
        artifact_dir=artifacts,
        extra_env={
            "MYCODE_API_KEY": "secret-value",
            "MYCODE_MODEL": "claude-test",
            "MYCODE_PROTOCOL": "anthropic-messages",
        },
    )


def test_manifest_preflight_rejects_hash_mismatch(tmp_path: Path) -> None:
    artifacts = _artifacts(tmp_path)
    load_artifact_manifest(artifacts)
    next(artifacts.glob("*.whl")).write_bytes(b"changed")

    with pytest.raises(ValueError, match="SHA-256"):
        load_artifact_manifest(artifacts)


def test_manifest_rejects_uv_hash_mismatch(tmp_path: Path) -> None:
    artifacts = _artifacts(tmp_path)
    (artifacts / "uv").write_bytes(b"changed")

    with pytest.raises(ValueError, match="uv binary SHA-256"):
        load_artifact_manifest(artifacts)


def test_provider_settings_are_native_and_explicit(tmp_path: Path) -> None:
    agent = _agent(tmp_path)

    assert agent.MODEL_CONNECTION is None
    model_info = agent.to_agent_info().model_info
    assert model_info is not None
    assert model_info.name == "claude-test"
    assert model_info.provider is None
    assert agent._provider_settings(agent.extra_env) == (
        "secret-value",
        "claude-test",
        "anthropic-messages",
        "",
    )
    with pytest.raises(ValueError, match="MYCODE_MODEL"):
        agent._provider_settings(
            {
                "MYCODE_API_KEY": "secret-value",
                "MYCODE_PROTOCOL": "anthropic-messages",
            }
        )


def test_terminal_result_classification_uses_machine_result_only() -> None:
    assert isinstance(
        MyCodeAgent._failure(3, {"outcome": "max_steps"}), MyCodeMaxStepsError
    )
    assert isinstance(MyCodeAgent._failure(1, None), MyCodeIncompleteError)
    assert isinstance(
        MyCodeAgent._failure(
            2,
            {
                "outcome": "failed",
                "error": {"code": "SettingsFileError", "message": "invalid"},
            },
        ),
        MyCodeConfigurationError,
    )


def test_context_projection_and_atif_validation(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    result = {
        "outcome": "succeeded",
        "session_id": "session-1",
        "run_id": "run-1",
        "invocation_id": "invocation-1",
        "completed_steps": 1,
        "evaluation": {"test_case_id": "case-1"},
        "usage": {
            "total_input_tokens": 15,
            "cache_read_input_tokens": 4,
            "cache_creation_input_tokens": 3,
            "output_tokens": 2,
        },
    }
    (agent.logs_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
    dto = {
        "session_id": "session-1",
        "agent": {"model": "claude-test", "provider": "harbor"},
        "steps": [
            {"kind": "user", "message": "task", "entry_id": "u1"},
            {
                "kind": "agent",
                "message": "done",
                "entry_id": "a1",
                "request_id": "r1",
                "tool_calls": [],
                "reasoning_disclosures": [],
                "usage": {
                    "prompt_tokens": 15,
                    "completion_tokens": 2,
                    "cache_read_tokens": 4,
                    "cache_write_tokens": 3,
                },
            },
        ],
        "evidence_gaps": [],
    }
    (agent.logs_dir / "mycode-trajectory.json").write_text(
        json.dumps(dto), encoding="utf-8"
    )
    context = AgentContext()

    agent.populate_context_post_run(context)

    assert context.n_input_tokens == 15
    assert context.n_cache_tokens == 4
    assert context.n_output_tokens == 2
    metadata = cast(dict[str, object], context.metadata)
    assert metadata["cache_creation_input_tokens"] == 3
    trajectory = json.loads((agent.logs_dir / "trajectory.json").read_text())
    assert trajectory["schema_version"] == "ATIF-v1.7"
    assert _trajectory_to_atif(dto, agent_version="0.1.0").steps[1].message == "done"


def test_instruction_and_api_key_are_not_cli_arguments(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    instruction = "fix 'quotes'; echo $PRIVATE && $(touch /tmp/nope)"
    arguments = agent._run_arguments("trial-1")

    assert instruction not in arguments
    assert "secret-value" not in arguments
    assert "--ignore-project-settings" in arguments


def test_job_analyzer_combines_harbor_and_agent_results(tmp_path: Path) -> None:
    trial = tmp_path / "trial-1"
    (trial / "agent").mkdir(parents=True)
    (trial / "result.json").write_text(
        json.dumps(
            {
                "task_name": "case-1",
                "trial_name": "case-1__1",
                "verifier_result": {"rewards": {"reward": 1}},
            }
        ),
        encoding="utf-8",
    )
    (trial / "agent" / "result.json").write_text(
        json.dumps(
            {
                "outcome": "succeeded",
                "session_id": "session-1",
                "completed_steps": 2,
                "duration_ms": 10,
                "usage": {
                    "total_input_tokens": 10,
                    "cache_read_input_tokens": 4,
                    "cache_creation_input_tokens": 2,
                    "output_tokens": 3,
                },
            }
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [sys.executable, "scripts/analyze_harbor_job.py", str(tmp_path)],
        cwd=Path(__file__).parents[2],
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(completed.stdout)

    assert report["summary"]["pass_rate"] == 1
    assert report["summary"]["token_p50"] == 13
    assert report["trials"][0]["cache_hit_ratio"] == 0.4
