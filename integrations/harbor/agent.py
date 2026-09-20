"""将固定 my-code artifact 接入 Harbor v0.23.0。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import tempfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any, override

from harbor.agents.capabilities import AgentCapabilities
from harbor.agents.installed.base import (
    AgentAuthenticationError,
    BaseInstalledAgent,
    NonZeroAgentExitCodeError,
    UnknownApiError,
    with_prompt_template,
)
from harbor.agents.options import InstalledAgentOptions
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.trajectories import Trajectory
from pydantic import Field

_INSTALL_BASE = PurePosixPath("/installed-agent/mycode")
_INSTALL_ROOT = _INSTALL_BASE / "venv"
_ARTIFACT_ROOT = _INSTALL_BASE / "artifacts"
_UV_BIN = _INSTALL_BASE / "uv"
_CONFIGURE_CODE = """\
import os
from pathlib import Path
from my_code.auth.credentials import CredentialStore
from my_code.config.paths import MyCodePaths, SettingsScope
from my_code.config.providers import ProviderProfile, ProviderProfileStore
from my_code.config.providers import ProviderProtocol
from my_code.config.store import SettingsLayer, SettingsStore
p = MyCodePaths.discover(Path.cwd(), environ=os.environ)
SettingsStore(p).write(SettingsScope.USER, SettingsLayer(active_provider='harbor'))
ProviderProfileStore(p.providers_path).write((ProviderProfile(
    'harbor', ProviderProtocol(os.environ['MYCODE_PROTOCOL']),
    os.environ['MYCODE_MODEL'], os.environ.get('MYCODE_BASE_URL') or None,
),))
CredentialStore(p.credentials_path).save_api_key(os.environ['MYCODE_API_KEY'], 'harbor')
"""
_PROJECT_RESULT_CODE = """\
import json
from pathlib import Path
source = Path('/logs/agent/stream.jsonl')
records = []
if source.exists():
    for line in source.read_text(encoding='utf-8').splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if value.get('type') == 'result':
            records.append(value)
target = Path('/logs/agent/result.json')
if records:
    target.write_text(json.dumps(records[-1], ensure_ascii=False, indent=2) + '\\n')
"""


class MyCodeMaxStepsError(NonZeroAgentExitCodeError):
    """Agent 命中确定性的 step 上限。"""


class MyCodeTimeoutError(NonZeroAgentExitCodeError):
    """my-code 内部 wall-clock timeout。"""


class MyCodeConfigurationError(NonZeroAgentExitCodeError):
    """my-code 启动或配置失败。"""


class MyCodeIncompleteError(NonZeroAgentExitCodeError):
    """进程终止前没有产生 terminal result。"""


class MyCodeAgentOptions(InstalledAgentOptions):
    artifact_dir: Path = Field(description="包含 wheel、constraints 和 manifest 的目录")
    max_steps: int | None = Field(default=None, gt=0)
    max_output_tokens: int | None = Field(default=None, gt=0)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_artifact_manifest(
    directory: Path,
) -> tuple[dict[str, Any], Path, Path, Path]:
    """校验不可变 artifact 集，防止 adapter 安装了非预期代码。"""

    root = directory.expanduser().resolve()
    manifest_path = root / "manifest.json"
    constraints = root / "constraints.txt"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Invalid my-code artifact manifest: {error}") from error
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("my-code artifact manifest must use schema_version 1")
    wheel_name = manifest.get("wheel")
    if not isinstance(wheel_name, str) or Path(wheel_name).name != wheel_name:
        raise ValueError("Artifact manifest wheel must be a plain filename")
    wheel = root / wheel_name
    uv_name = manifest.get("uv")
    if not isinstance(uv_name, str) or Path(uv_name).name != uv_name:
        raise ValueError("Artifact manifest uv must be a plain filename")
    uv_binary = root / uv_name
    for path in (wheel, constraints, uv_binary):
        if not path.is_file():
            raise ValueError(f"Missing my-code artifact: {path.name}")
    expected = manifest.get("wheel_sha256")
    if not isinstance(expected, str) or _sha256(wheel) != expected:
        raise ValueError("my-code wheel SHA-256 mismatch")
    expected_constraints = manifest.get("constraints_sha256")
    if (
        not isinstance(expected_constraints, str)
        or _sha256(constraints) != expected_constraints
    ):
        raise ValueError("my-code constraints SHA-256 mismatch")
    expected_uv = manifest.get("uv_sha256")
    if not isinstance(expected_uv, str) or _sha256(uv_binary) != expected_uv:
        raise ValueError("uv binary SHA-256 mismatch")
    python_version = manifest.get("python_version")
    if (
        not isinstance(python_version, str)
        or re.fullmatch(r"3\.12(?:\.\d+)?", python_version) is None
    ):
        raise ValueError("Artifact Python version must select Python 3.12")
    return manifest, wheel, constraints, uv_binary


class MyCodeAgent(BaseInstalledAgent):
    """只接受本地构建产物、只支持 my-code 原生两种 provider 协议。"""

    capabilities = AgentCapabilities(atif=True)
    options_model = MyCodeAgentOptions
    options: MyCodeAgentOptions

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        (
            self._manifest,
            self._wheel,
            self._constraints,
            self._uv_binary,
        ) = load_artifact_manifest(self.options.artifact_dir)

    @staticmethod
    @override
    def name() -> str:
        return "mycode"

    @classmethod
    @override
    def preflight(
        cls,
        kwargs: dict[str, Any] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        super().preflight(kwargs, env)
        options = cls.parse_options(kwargs, env)
        assert isinstance(options, MyCodeAgentOptions)
        load_artifact_manifest(options.artifact_dir)
        available = {**os.environ, **dict(env or {})}
        cls._provider_settings(available)

    @override
    def get_version_command(self) -> str | None:
        return f"{_INSTALL_ROOT}/bin/mycode --version"

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        remote_wheel = str(_ARTIFACT_ROOT / self._wheel.name)
        remote_constraints = str(_ARTIFACT_ROOT / "constraints.txt")
        await self.exec_as_root(
            environment,
            command=f"mkdir -p {shlex.quote(str(_ARTIFACT_ROOT))}",
        )
        await environment.upload_file(self._wheel, remote_wheel)
        await environment.upload_file(self._constraints, remote_constraints)
        await environment.upload_file(self._uv_binary, str(_UV_BIN))
        python_version = str(self._manifest["python_version"])
        await self.exec_as_root(
            environment,
            command=(
                f"chmod 0755 {shlex.quote(str(_UV_BIN))} && "
                f"{_UV_BIN} python install {shlex.quote(python_version)} && "
                f"{_UV_BIN} venv --python {shlex.quote(python_version)} "
                f"--managed-python {shlex.quote(str(_INSTALL_ROOT))} "
                "&& "
                f"{_UV_BIN} pip install --python {_INSTALL_ROOT}/bin/python "
                f"--constraint {shlex.quote(remote_constraints)} "
                f"{shlex.quote(remote_wheel)} && "
                f"{_INSTALL_ROOT}/bin/mycode --version"
            ),
            env={
                "UV_CACHE_DIR": str(_INSTALL_BASE / "cache"),
                "UV_NO_PROGRESS": "1",
                "UV_PYTHON_INSTALL_DIR": str(_INSTALL_BASE / "python"),
            },
        )

    @staticmethod
    def _provider_settings(source: Mapping[str, str]) -> tuple[str, str, str, str]:
        """读取 my-code 原生连接配置，不委托 Harbor 推断 provider。"""

        api_key = source.get("MYCODE_API_KEY", "").strip()
        model = source.get("MYCODE_MODEL", "").strip()
        protocol = source.get("MYCODE_PROTOCOL", "").strip()
        base_url = source.get("MYCODE_BASE_URL", "").strip()
        if not api_key:
            raise ValueError("MyCodeAgent requires MYCODE_API_KEY")
        if not model:
            raise ValueError("MyCodeAgent requires MYCODE_MODEL")
        if protocol not in {"anthropic-messages", "openai-responses"}:
            raise ValueError(
                "MYCODE_PROTOCOL must be anthropic-messages or openai-responses"
            )
        return api_key, model, protocol, base_url

    def _run_arguments(self, trial_id: str) -> list[str]:
        arguments = [
            f"{_INSTALL_ROOT}/bin/mycode",
            "run",
            "--output-format",
            "stream-json",
            "--ignore-project-settings",
            "--dangerously-skip-permissions",
            "--sandbox-mode",
            "local",
            "--evaluation-run-id",
            trial_id,
            "--test-case-id",
            self.session_id or trial_id,
            "--attempt-id",
            self.session_id or trial_id,
        ]
        if self.options.max_steps is not None:
            arguments.extend(("--max-steps", str(self.options.max_steps)))
        if self.options.max_output_tokens is not None:
            arguments.extend(
                ("--max-output-tokens", str(self.options.max_output_tokens))
            )
        return arguments

    @with_prompt_template
    @override
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        del context
        api_key, model, protocol, base_url = self._provider_settings(
            {**os.environ, **self.extra_env}
        )
        trial_id = str(self.context_id or self.session_id or "trial")
        runtime_token = hashlib.sha256(trial_id.encode()).hexdigest()[:20]
        runtime_root = PurePosixPath("/tmp") / f"mycode-{runtime_token}"
        config_root = runtime_root / "config"
        instruction_path = runtime_root / "instruction.txt"
        log_root = self.environment_logs_dir
        env = {
            "MY_CODE_CONFIG_DIR": str(config_root),
            "MYCODE_API_KEY": api_key,
            "MYCODE_PROTOCOL": protocol,
            "MYCODE_MODEL": model,
            "MYCODE_BASE_URL": base_url,
        }
        result: dict[str, Any] | None = None
        try:
            await environment.exec(command=f"mkdir -p {shlex.quote(str(runtime_root))}")
            with tempfile.TemporaryDirectory(prefix="harbor-mycode-") as temp_dir:
                local_instruction = Path(temp_dir) / "instruction.txt"
                local_instruction.write_text(instruction, encoding="utf-8")
                await environment.upload_file(local_instruction, str(instruction_path))
            await self.exec_as_agent(
                environment,
                command=(
                    f"mkdir -p {shlex.quote(str(config_root))} "
                    f"{shlex.quote(str(log_root / 'mycode' / 'projects'))} && "
                    f"ln -s {shlex.quote(str(log_root / 'mycode' / 'projects'))} "
                    f"{shlex.quote(str(config_root / 'projects'))} && "
                    f"{_INSTALL_ROOT}/bin/python -c {shlex.quote(_CONFIGURE_CODE)}"
                ),
                env=env,
            )
            arguments = self._run_arguments(trial_id)
            command = (
                f"{shlex.join(arguments)} < {shlex.quote(str(instruction_path))} "
                f"> {shlex.quote(str(log_root / 'stream.jsonl'))} "
                f"2> {shlex.quote(str(log_root / 'stderr.log'))}"
            )
            completed = await environment.exec(command=command, env=env)
            await environment.exec(
                command=(
                    f"{_INSTALL_ROOT}/bin/python -c {shlex.quote(_PROJECT_RESULT_CODE)}"
                )
            )
            result = await self._read_remote_result(environment)
            if result is not None:
                await self._export_remote_trajectory(environment, result)
            if completed.return_code != 0:
                raise self._failure(completed.return_code, result)
        finally:
            await environment.exec(
                command=(
                    f"rm -f {shlex.quote(str(instruction_path))}; "
                    f"rm -rf {shlex.quote(str(config_root))}"
                )
            )

    async def _read_remote_result(
        self, environment: BaseEnvironment
    ) -> dict[str, Any] | None:
        with tempfile.TemporaryDirectory(prefix="harbor-mycode-result-") as temp_dir:
            local = Path(temp_dir) / "result.json"
            try:
                await environment.download_file("/logs/agent/result.json", local)
                value = json.loads(local.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                return None
        return value if isinstance(value, dict) else None

    async def _export_remote_trajectory(
        self, environment: BaseEnvironment, result: dict[str, Any]
    ) -> None:
        artifacts = result.get("artifacts")
        session_log = (
            artifacts.get("session_log") if isinstance(artifacts, dict) else None
        )
        session_id = result.get("session_id")
        if not isinstance(session_log, str) or not isinstance(session_id, str):
            return
        await environment.exec(
            command=shlex.join(
                [
                    f"{_INSTALL_ROOT}/bin/python",
                    "-m",
                    "my_code.sessions.trajectory",
                    str(Path(session_log).parent),
                    session_id,
                    "--output",
                    "/logs/agent/mycode-trajectory.json",
                ]
            )
        )

    @staticmethod
    def _failure(return_code: int, result: dict[str, Any] | None) -> Exception:
        if result is None:
            return MyCodeIncompleteError(
                f"mycode exited {return_code} without terminal result"
            )
        outcome = result.get("outcome")
        raw_error = result.get("error")
        error: dict[str, Any] = raw_error if isinstance(raw_error, dict) else {}
        code = str(error.get("code") or "")
        message = str(error.get("message") or outcome or "mycode failed")
        if outcome == "max_steps":
            return MyCodeMaxStepsError(message)
        if outcome == "timed_out" or code == "timeout":
            return MyCodeTimeoutError(message)
        lowered = f"{code} {message}".lower()
        if "auth" in lowered or "api key" in lowered or "401" in lowered:
            return AgentAuthenticationError(message)
        if code in {
            "ProviderConfigurationRequired",
            "ProviderProfileError",
            "SettingsFileError",
            "ValueError",
        }:
            return MyCodeConfigurationError(message)
        if "provider" in lowered or "api" in lowered:
            return UnknownApiError(message)
        return NonZeroAgentExitCodeError(message)

    @override
    def populate_context_post_run(self, context: AgentContext) -> None:
        result_path = self.logs_dir / "result.json"
        metadata: dict[str, Any] = {
            "artifact_version": self._manifest.get("version"),
            "artifact_wheel_sha256": self._manifest.get("wheel_sha256"),
            "artifact_lock_sha256": self._manifest.get("lock_sha256"),
            "artifact_python_version": self._manifest.get("python_version"),
            "artifact_uv_version": self._manifest.get("uv_version"),
            "artifact_uv_sha256": self._manifest.get("uv_sha256"),
        }
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            metadata["outcome"] = "incomplete"
            context.metadata = metadata
            return
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        context.n_input_tokens = int(usage.get("total_input_tokens") or 0)
        context.n_cache_tokens = int(usage.get("cache_read_input_tokens") or 0)
        context.n_output_tokens = int(usage.get("output_tokens") or 0)
        metadata.update(
            outcome=result.get("outcome"),
            completed_steps=result.get("completed_steps"),
            cache_creation_input_tokens=usage.get("cache_creation_input_tokens", 0),
            session_id=result.get("session_id"),
            run_id=result.get("run_id"),
            invocation_id=result.get("invocation_id"),
            evaluation=result.get("evaluation"),
        )
        context.metadata = metadata
        self._convert_trajectory(metadata)

    def _convert_trajectory(self, metadata: dict[str, Any]) -> None:
        source = self.logs_dir / "mycode-trajectory.json"
        target = self.logs_dir / "trajectory.json"
        try:
            dto = json.loads(source.read_text(encoding="utf-8"))
            trajectory = _trajectory_to_atif(
                dto,
                agent_version=str(self._manifest.get("version") or "unknown"),
            )
            target.write_text(
                json.dumps(trajectory.to_json_dict(), ensure_ascii=False, indent=2)
                + "\n",
                encoding="utf-8",
            )
        except Exception as error:
            metadata["trajectory_conversion_error"] = type(error).__name__


def _trajectory_to_atif(dto: dict[str, Any], *, agent_version: str) -> Trajectory:
    """在 Harbor adapter 内完成 DTO 到 ATIF-v1.7 的唯一技术耦合。"""

    raw_agent = dto.get("agent")
    agent: dict[str, Any] = raw_agent if isinstance(raw_agent, dict) else {}
    output_steps: list[dict[str, Any]] = []
    agent_step_by_entry: dict[str, dict[str, Any]] = {}
    for raw in dto.get("steps") or []:
        if not isinstance(raw, dict):
            continue
        kind = raw.get("kind")
        if kind == "user":
            output_steps.append(
                {
                    "step_id": len(output_steps) + 1,
                    "timestamp": raw.get("timestamp"),
                    "source": "user",
                    "message": raw.get("message") or "",
                }
            )
        elif kind == "agent":
            raw_usage = raw.get("usage")
            usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
            cache_write = usage.get("cache_write_tokens") or 0
            step: dict[str, Any] = {
                "step_id": len(output_steps) + 1,
                "timestamp": raw.get("timestamp"),
                "source": "agent",
                "message": raw.get("message") or "",
                "reasoning_content": raw.get("reasoning"),
                "tool_calls": [
                    {
                        "tool_call_id": call.get("call_id"),
                        "function_name": call.get("name"),
                        "arguments": call.get("arguments") or {},
                    }
                    for call in raw.get("tool_calls") or []
                    if isinstance(call, dict)
                ]
                or None,
                "metrics": {
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens"),
                    "cached_tokens": usage.get("cache_read_tokens"),
                    "extra": {"cache_write_tokens": cache_write},
                },
                "llm_call_count": 1,
                "extra": {
                    "request_id": raw.get("request_id"),
                    "reasoning_disclosures": raw.get("reasoning_disclosures") or [],
                },
            }
            output_steps.append(step)
            entry_id = raw.get("entry_id")
            if isinstance(entry_id, str):
                agent_step_by_entry[entry_id] = step
        elif kind == "observation":
            parent = agent_step_by_entry.get(str(raw.get("source_assistant_id")))
            if parent is None:
                continue
            parent["observation"] = {
                "results": [
                    {
                        "source_call_id": result.get("call_id"),
                        "content": result.get("content"),
                        "extra": {"is_error": bool(result.get("is_error"))},
                    }
                    for result in raw.get("results") or []
                    if isinstance(result, dict)
                ]
            }
    prompt_tokens = completion_tokens = cached_tokens = cache_write_tokens = 0
    for step in output_steps:
        metrics = step.get("metrics")
        if not isinstance(metrics, dict):
            continue
        prompt_tokens += int(metrics.get("prompt_tokens") or 0)
        completion_tokens += int(metrics.get("completion_tokens") or 0)
        cached_tokens += int(metrics.get("cached_tokens") or 0)
        extra = metrics.get("extra")
        if isinstance(extra, dict):
            cache_write_tokens += int(extra.get("cache_write_tokens") or 0)
    return Trajectory.model_validate(
        {
            "schema_version": "ATIF-v1.7",
            "session_id": dto.get("session_id"),
            "agent": {
                "name": "mycode",
                "version": agent_version,
                "model_name": agent.get("model"),
                "extra": {
                    "provider": agent.get("provider"),
                    "protocol": agent.get("protocol"),
                },
            },
            "steps": output_steps,
            "final_metrics": {
                "total_prompt_tokens": prompt_tokens,
                "total_completion_tokens": completion_tokens,
                "total_cached_tokens": cached_tokens,
                "total_steps": len(output_steps),
                "extra": {"cache_write_tokens": cache_write_tokens},
            },
            "extra": {"evidence_gaps": dto.get("evidence_gaps") or []},
            "notes": "Converted from canonical my-code Session evidence",
        }
    )


__all__ = [
    "MyCodeAgent",
    "MyCodeAgentOptions",
    "MyCodeConfigurationError",
    "MyCodeIncompleteError",
    "MyCodeMaxStepsError",
    "MyCodeTimeoutError",
    "load_artifact_manifest",
]
