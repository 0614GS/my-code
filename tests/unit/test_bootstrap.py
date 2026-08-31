"""Composition-root storage and entrypoint tests."""

import json
import stat
from collections.abc import Callable
from dataclasses import replace
from io import StringIO
from pathlib import Path

import pytest

import my_code.bootstrap as bootstrap_module
from my_code.auth.credentials import CredentialSource, CredentialStore
from my_code.bootstrap import (
    ApplicationAssembly,
    _assemble_agent,
    initialize_user_storage,
    main,
)
from my_code.config.paths import MyCodePaths
from my_code.config.providers import (
    ProviderProfile,
    ProviderProfileStore,
    ProviderProtocol,
)
from my_code.config.settings import AgentSettings
from my_code.config.store import McpServerSettingsLayer
from my_code.foundation.json import JsonObject
from my_code.mcp.models import (
    McpCallResult,
    McpConnectionInfo,
    McpRemoteTool,
    McpServerSpec,
)
from my_code.mcp.transport import McpTransport
from my_code.permissions.models import PermissionMode


def make_paths(tmp_path: Path) -> MyCodePaths:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return MyCodePaths.discover(workspace, environ={}, home=tmp_path / "home")


def make_settings(tmp_path: Path) -> AgentSettings:
    paths = make_paths(tmp_path)
    return AgentSettings(
        paths=paths,
        provider_id="anthropic",
        model="test-model",
        permission_mode=PermissionMode.DEFAULT,
        max_steps=3,
        max_output_tokens=1024,
        interactive=False,
        credential_source=CredentialSource.NONE,
    )


def test_agent_assembly_exposes_named_component_identities(tmp_path: Path) -> None:
    assembly = _assemble_agent(
        make_settings(tmp_path),
        "11111111-1111-1111-1111-111111111111",
    )

    assert isinstance(assembly, ApplicationAssembly)
    assert not isinstance(assembly, tuple)
    assert assembly.tool_executor.tools == assembly.initial_tools
    assert assembly.tool_catalog.snapshot() == assembly.initial_tools
    assert assembly.tool_executor.policy is assembly.permissions
    assert assembly.tool_executor.context is assembly.tool_context
    assert assembly.provider_runtime.router is assembly.provider
    assert assembly.session.session_id == "11111111-1111-1111-1111-111111111111"


def test_subagent_tool_registration_is_feature_gated(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    disabled = _assemble_agent(
        replace(settings, subagents_enabled=False),
        "22222222-2222-2222-2222-222222222222",
    )
    enabled = _assemble_agent(
        replace(
            settings,
            subagents_enabled=True,
            subagent_max_depth=2,
            subagent_max_active_children=3,
            subagent_max_steps=5,
            subagent_max_tokens=600,
            subagent_timeout_seconds=7.0,
        ),
        "33333333-3333-3333-3333-333333333333",
    )
    background = _assemble_agent(
        replace(
            settings,
            interactive=True,
        ),
        "44444444-4444-4444-4444-444444444444",
    )

    assert disabled.initial_tools.get("Subagent") is None
    tool = enabled.initial_tools.get("Subagent")
    assert tool is not None
    assert tool.definition.name == "Subagent"
    assert enabled.initial_tools.source_for("Subagent") is not None
    properties = tool.definition.input_schema["properties"]
    assert isinstance(properties, dict)
    assert "background" not in properties
    assert tuple(
        definition.name for definition in background.initial_tools.definitions
    ) == (
        "Bash",
        "Edit",
        "Glob",
        "Grep",
        "InvokeSearchedTool",
        "Read",
        "Subagent",
        "TaskCancel",
        "TaskList",
        "TodoWrite",
        "ToolSearch",
        "Write",
    )


class BootstrapMcpTransport:
    def __init__(self) -> None:
        self.closed = False
        self.tools_changed_handler: Callable[[], None] | None = None

    def set_tools_changed_handler(self, handler: Callable[[], None] | None) -> None:
        self.tools_changed_handler = handler

    async def connect(self, *, timeout_seconds: float) -> McpConnectionInfo:
        del timeout_seconds
        return McpConnectionInfo("2025-11-25", "fake", "1.0")

    async def list_tools(self, *, timeout_seconds: float) -> tuple[McpRemoteTool, ...]:
        del timeout_seconds
        return (
            McpRemoteTool(
                "lookup",
                "Lookup",
                {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            ),
        )

    async def call_tool(
        self,
        name: str,
        arguments: JsonObject,
        *,
        timeout_seconds: float,
    ) -> McpCallResult:
        del name, arguments, timeout_seconds
        return McpCallResult("ok")

    async def close(self) -> None:
        self.closed = True


def test_headless_ignores_background_task_configuration(tmp_path: Path) -> None:
    assembled = _assemble_agent(
        replace(
            make_settings(tmp_path),
            interactive=False,
            subagents_enabled=True,
            background_tasks_enabled=True,
        ),
        "55555555-5555-5555-5555-555555555555",
    )

    names = tuple(item.name for item in assembled.initial_tools.definitions)
    subagent = assembled.initial_tools.get("Subagent")

    assert subagent is not None
    properties = subagent.definition.input_schema["properties"]
    assert isinstance(properties, dict)
    assert "background" not in properties
    assert not {"TaskList", "TaskCancel"}.intersection(names)
    assert assembled.background_notifications is None
    assert assembled.background_wake_signal is None


def test_background_bash_does_not_require_subagents(tmp_path: Path) -> None:
    assembled = _assemble_agent(
        replace(
            make_settings(tmp_path),
            interactive=True,
            subagents_enabled=False,
            background_tasks_enabled=True,
        ),
        "56565656-5656-5656-5656-565656565656",
    )

    names = {item.name for item in assembled.initial_tools.definitions}
    bash = assembled.initial_tools.get("Bash")
    assert bash is not None
    properties = bash.definition.input_schema["properties"]
    assert isinstance(properties, dict)
    assert "background" in properties
    assert {"TaskList", "TaskCancel"}.issubset(names)
    assert "TaskOutput" not in names
    assert "Subagent" not in names


@pytest.mark.asyncio
async def test_mcp_registration_is_feature_gated_and_starts_before_turns(
    tmp_path: Path,
) -> None:
    settings = replace(
        make_settings(tmp_path),
        mcp_enabled=True,
        mcp_servers=(McpServerSettingsLayer("fake", "fake-server"),),
    )
    transport = BootstrapMcpTransport()

    def factory(spec: McpServerSpec) -> McpTransport:
        assert spec.name == "fake"
        return transport

    assembly = _assemble_agent(
        settings,
        "55555555-5555-5555-5555-555555555555",
        mcp_transport_factory=factory,
    )

    assert assembly.initial_tools.get("mcp__fake__lookup") is None
    await assembly.mcp.start()
    assert assembly.tool_catalog.snapshot().get("mcp__fake__lookup") is not None

    await assembly.mcp.close()
    assert transport.closed is True
    assert assembly.tool_catalog.snapshot().get("mcp__fake__lookup") is None


@pytest.mark.asyncio
async def test_skill_registration_is_feature_gated_and_lazy(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    skill_dir = settings.paths.project_config_dir / "skills" / "review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\ndescription: Review the current change\n---\nReview carefully.\n",
        encoding="utf-8",
    )
    disabled = _assemble_agent(
        settings,
        "66666666-6666-6666-6666-666666666666",
    )
    enabled = _assemble_agent(
        replace(settings, skills_enabled=True),
        "77777777-7777-7777-7777-777777777777",
    )

    await disabled.skills.start()
    await enabled.skills.start()

    assert disabled.tool_catalog.snapshot().get("Skill") is None
    assert enabled.tool_catalog.snapshot().get("Skill") is not None
    await disabled.skills.close()
    await enabled.skills.close()


def test_bootstrap_creates_required_user_layout_only(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)

    result = initialize_user_storage(paths)

    assert result.created_settings is True
    assert result.created_providers is True
    assert result.created_credentials is True
    assert json.loads(paths.user_settings_path.read_text(encoding="utf-8")) == {
        "version": 4,
    }
    assert ProviderProfileStore(paths.providers_path).load() == {}
    assert CredentialStore(paths.credentials_path).load_api_key("anthropic") is None
    assert paths.projects_dir.is_dir()
    assert not paths.project_settings_path.exists()
    assert stat.S_IMODE(paths.config_home.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.credentials_path.stat().st_mode) == 0o600


def test_bootstrap_is_idempotent_and_preserves_existing_profiles(
    tmp_path: Path,
) -> None:
    paths = make_paths(tmp_path)
    initialize_user_storage(paths)
    original = paths.providers_path.read_text(encoding="utf-8")

    result = initialize_user_storage(paths)

    assert result.created_settings is False
    assert result.created_providers is False
    assert result.created_credentials is False
    assert paths.providers_path.read_text(encoding="utf-8") == original


def test_bootstrap_preserves_existing_historical_anthropic_profile(
    tmp_path: Path,
) -> None:
    paths = make_paths(tmp_path)
    profile = ProviderProfile(
        "anthropic",
        ProviderProtocol.ANTHROPIC_MESSAGES,
        "user-customized-model",
    )
    ProviderProfileStore(paths.providers_path).write((profile,))

    initialize_user_storage(paths)

    assert ProviderProfileStore(paths.providers_path).load() == {"anthropic": profile}


def test_bootstrap_rejects_legacy_storage_without_modifying_it(
    tmp_path: Path,
) -> None:
    paths = make_paths(tmp_path)
    paths.config_home.mkdir(parents=True)
    paths.user_settings_path.write_text(
        json.dumps(
            {
                "model": "legacy-model",
                "baseUrl": "https://legacy.example/api",
            }
        ),
        encoding="utf-8",
    )
    paths.credentials_path.write_text(
        json.dumps({"anthropicApiKey": "legacy-key"}), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="incompatible schema"):
        initialize_user_storage(paths)

    assert "legacy-model" in paths.user_settings_path.read_text(encoding="utf-8")
    assert "anthropicApiKey" in paths.credentials_path.read_text(encoding="utf-8")


class TerminalStream(StringIO):
    def __init__(self, interactive: bool) -> None:
        super().__init__()
        self.interactive = interactive

    def isatty(self) -> bool:
        return self.interactive


@pytest.mark.parametrize("stdin_tty, stdout_tty", ((False, True), (True, False)))
def test_cli_launch_rejects_non_tty_before_initialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stdin_tty: bool,
    stdout_tty: bool,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_home = tmp_path / "config"
    monkeypatch.setenv("MY_CODE_CONFIG_DIR", str(config_home))
    monkeypatch.setattr(bootstrap_module.sys, "stdin", TerminalStream(stdin_tty))
    monkeypatch.setattr(bootstrap_module.sys, "stdout", TerminalStream(stdout_tty))
    errors = TerminalStream(False)
    monkeypatch.setattr(bootstrap_module.sys, "stderr", errors)
    monkeypatch.setattr(
        bootstrap_module,
        "initialize_user_storage",
        lambda _paths: pytest.fail("storage must not be initialized"),
    )
    monkeypatch.setattr(
        bootstrap_module,
        "discover_active_model",
        lambda _settings: pytest.fail("provider discovery must not run"),
    )
    monkeypatch.setattr(
        bootstrap_module,
        "MyCodeTui",
        lambda _runtime: pytest.fail("TUI must not start"),
    )

    with pytest.raises(SystemExit) as exit_info:
        main(["--cwd", str(workspace)])

    assert exit_info.value.code == 2
    assert errors.getvalue() == (
        "Error: mycode requires an interactive terminal; "
        "piped or redirected chat is not supported.\n"
    )
    assert not config_home.exists()


@pytest.mark.parametrize("argument", ("--help", "--version"))
def test_cli_metadata_options_work_without_tty(
    monkeypatch: pytest.MonkeyPatch, argument: str
) -> None:
    output = TerminalStream(False)
    monkeypatch.setattr(bootstrap_module.sys, "stdin", TerminalStream(False))
    monkeypatch.setattr(bootstrap_module.sys, "stdout", output)

    with pytest.raises(SystemExit) as exit_info:
        main([argument])

    assert exit_info.value.code == 0
    assert output.getvalue()
