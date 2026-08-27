"""Layered configuration store tests."""

import json
import stat
from pathlib import Path

import pytest

from my_code.config.paths import MyCodePaths, SettingsScope
from my_code.config.settings import SettingsResolver
from my_code.config.store import (
    McpServerSettingsLayer,
    SettingsFileError,
    SettingsLayer,
    SettingsStore,
)
from my_code.model.tool_search import ToolSearchMode
from my_code.permissions.models import (
    PermissionBehavior,
    PermissionMode,
    PermissionRule,
)


def make_paths(tmp_path: Path) -> MyCodePaths:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return MyCodePaths(cwd=workspace.resolve(), config_home=tmp_path / "state")


def test_load_merges_user_project_and_local_precedence(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    store = SettingsStore(paths)
    store.write(
        SettingsScope.USER,
        SettingsLayer(
            model="user-model",
            permission_mode=PermissionMode.PLAN,
            max_steps=10,
            max_parallel_tool_calls=2,
        ),
    )
    store.write(
        SettingsScope.PROJECT,
        SettingsLayer(
            model="project-model",
            max_output_tokens=4000,
            max_parallel_tool_calls=3,
        ),
    )
    store.write(
        SettingsScope.LOCAL,
        SettingsLayer(
            permission_mode=PermissionMode.ACCEPT_EDITS,
            max_steps=20,
            max_parallel_tool_calls=1,
        ),
    )

    assert store.load() == SettingsLayer(
        model="project-model",
        permission_mode=PermissionMode.ACCEPT_EDITS,
        max_steps=20,
        max_output_tokens=4000,
        max_parallel_tool_calls=1,
    )
    user_document = json.loads(paths.user_settings_path.read_text(encoding="utf-8"))
    assert user_document["agent"]["maxSteps"] == 10
    assert user_document["tools"]["maxParallelCalls"] == 2
    assert "maxTurns" not in user_document["agent"]


def test_empty_and_missing_files_are_empty_layers(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    paths.project_settings_path.parent.mkdir()
    paths.project_settings_path.write_text("\n", encoding="utf-8")

    assert SettingsStore(paths).load() == SettingsLayer()


def test_project_layers_are_skipped_when_started_from_user_home(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    paths = MyCodePaths(cwd=home, config_home=home / ".my-code")
    store = SettingsStore(paths)
    store.write(
        SettingsScope.USER,
        SettingsLayer(active_provider="gateway", model="user-model"),
    )
    paths.local_settings_path.write_text(
        json.dumps({"version": 3, "agent": {"model": "misclassified-local-model"}}),
        encoding="utf-8",
    )

    assert store.load_scope(SettingsScope.PROJECT) == SettingsLayer()
    assert store.load_scope(SettingsScope.LOCAL) == SettingsLayer()
    assert store.load() == SettingsLayer(active_provider="gateway", model="user-model")


@pytest.mark.parametrize("scope", [SettingsScope.PROJECT, SettingsScope.LOCAL])
def test_project_writes_cannot_overwrite_colliding_user_storage(
    tmp_path: Path, scope: SettingsScope
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    paths = MyCodePaths(cwd=home, config_home=home / ".my-code")

    with pytest.raises(SettingsFileError, match="project config directory"):
        SettingsStore(paths).write(scope, SettingsLayer(model="project-model"))


def test_unknown_keys_are_ignored_for_forward_compatibility(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    paths.user_settings_path.parent.mkdir()
    paths.user_settings_path.write_text(
        json.dumps(
            {
                "version": 3,
                "agent": {"model": "known"},
                "futureSetting": {"enabled": True},
            }
        ),
        encoding="utf-8",
    )

    assert SettingsStore(paths).load().model == "known"


def test_subagent_settings_layer_and_runtime_defaults(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    store = SettingsStore(paths)
    store.write(
        SettingsScope.USER,
        SettingsLayer(
            subagents_enabled=True,
            subagent_max_depth=2,
            subagent_max_active_children=3,
            subagent_max_steps=8,
            subagent_max_tokens=900,
            subagent_timeout_seconds=12.5,
            background_tasks_enabled=True,
        ),
    )

    document = json.loads(paths.user_settings_path.read_text(encoding="utf-8"))
    assert document["subagents"] == {
        "enabled": True,
        "maxDepth": 2,
        "maxActiveChildren": 3,
        "maxSteps": 8,
        "maxTokens": 900,
        "timeoutSeconds": 12.5,
    }
    assert document["backgroundTasks"] == {"enabled": True}
    settings = SettingsResolver(paths).resolve(interactive=False)
    assert settings.subagents_enabled is True
    assert settings.subagent_max_depth == 2
    assert settings.subagent_max_active_children == 3
    assert settings.subagent_max_steps == 8
    assert settings.subagent_max_tokens == 900
    assert settings.subagent_timeout_seconds == 12.5
    assert settings.background_tasks_enabled is True


def test_subagents_and_background_tasks_are_enabled_by_default(tmp_path: Path) -> None:
    settings = SettingsResolver(make_paths(tmp_path)).resolve(interactive=False)

    assert settings.subagents_enabled is True
    assert settings.background_tasks_enabled is True
    assert settings.skills_enabled is False
    assert settings.mcp_enabled is False
    assert settings.mcp_servers == ()
    assert settings.tool_search_mode is ToolSearchMode.DISPATCHER


def test_subagents_and_background_tasks_can_be_explicitly_disabled(
    tmp_path: Path,
) -> None:
    paths = make_paths(tmp_path)
    SettingsStore(paths).write(
        SettingsScope.USER,
        SettingsLayer(
            subagents_enabled=False,
            background_tasks_enabled=False,
        ),
    )

    settings = SettingsResolver(paths).resolve(interactive=True)

    assert settings.subagents_enabled is False
    assert settings.background_tasks_enabled is False


def test_skill_feature_gate_is_layered_and_serialized(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    store = SettingsStore(paths)
    store.write(SettingsScope.USER, SettingsLayer(skills_enabled=True))

    document = json.loads(paths.user_settings_path.read_text(encoding="utf-8"))

    assert document["skills"] == {"enabled": True}
    assert SettingsResolver(paths).resolve(interactive=False).skills_enabled is True


def test_mcp_settings_replace_servers_by_scope_and_store_only_env_references(
    tmp_path: Path,
) -> None:
    paths = make_paths(tmp_path)
    store = SettingsStore(paths)
    store.write(
        SettingsScope.USER,
        SettingsLayer(
            mcp_enabled=True,
            tool_search_mode=ToolSearchMode.NATIVE,
            mcp_servers=(
                McpServerSettingsLayer(
                    "user-server",
                    "/usr/bin/user-server",
                    ("--stdio",),
                    (("TOKEN", "USER_SERVER_TOKEN"),),
                ),
            ),
        ),
    )
    store.write(
        SettingsScope.PROJECT,
        SettingsLayer(
            mcp_servers=(
                McpServerSettingsLayer(
                    "project-server",
                    "project-server",
                    enabled=False,
                    scope=SettingsScope.PROJECT,
                ),
            ),
        ),
    )
    store.write(
        SettingsScope.LOCAL,
        SettingsLayer(
            mcp_servers=(
                McpServerSettingsLayer(
                    "project-server",
                    "/trusted/project-server",
                    enabled=True,
                    scope=SettingsScope.LOCAL,
                ),
            ),
        ),
    )

    resolved = SettingsResolver(
        paths, environ={"USER_SERVER_TOKEN": "super-secret-value"}
    ).resolve(interactive=False)
    assert resolved.mcp_enabled is True
    assert resolved.tool_search_mode is ToolSearchMode.NATIVE
    assert tuple(server.name for server in resolved.mcp_servers) == (
        "project-server",
        "user-server",
    )
    project = resolved.mcp_servers[0]
    assert project.command == "/trusted/project-server"
    assert project.scope is SettingsScope.LOCAL
    user_document = json.loads(paths.user_settings_path.read_text(encoding="utf-8"))
    assert user_document["tools"]["toolSearchMode"] == "native"
    assert user_document["mcp"]["servers"]["user-server"]["envFrom"] == {
        "TOKEN": "USER_SERVER_TOKEN"
    }
    assert "super-secret-value" not in json.dumps(user_document)


def test_shared_project_mcp_definition_is_disabled_unless_copied_locally(
    tmp_path: Path,
) -> None:
    paths = make_paths(tmp_path)
    paths.project_settings_path.parent.mkdir()
    paths.project_settings_path.write_text(
        json.dumps(
            {
                "version": 3,
                "mcp": {"servers": {"project-server": {"command": "project-server"}}},
            }
        ),
        encoding="utf-8",
    )

    server = SettingsStore(paths).load_scope(SettingsScope.PROJECT).mcp_servers[0]
    assert server.enabled is False
    assert server.scope is SettingsScope.PROJECT


@pytest.mark.parametrize(
    "mcp, message",
    [
        ({"enabled": True}, "cannot enable MCP execution"),
        (
            {
                "servers": {
                    "project-server": {
                        "command": "project-server",
                        "enabled": True,
                    }
                }
            },
            "cannot be enabled directly",
        ),
    ],
)
def test_shared_project_cannot_activate_mcp(
    tmp_path: Path, mcp: object, message: str
) -> None:
    paths = make_paths(tmp_path)
    paths.project_settings_path.parent.mkdir()
    paths.project_settings_path.write_text(
        json.dumps({"version": 3, "mcp": mcp}),
        encoding="utf-8",
    )

    with pytest.raises(SettingsFileError, match=message):
        SettingsStore(paths).load_scope(SettingsScope.PROJECT)


@pytest.mark.parametrize(
    "document, message",
    [
        ([], "root must be an object"),
        (
            {"version": 3, "agent": {"maxSteps": True}},
            "agent.maxSteps must be a positive integer",
        ),
        (
            {"version": 3, "agent": {"maxTurns": 3}},
            "agent.maxTurns is no longer supported",
        ),
        ({"version": 3, "permissions": []}, "permissions must be an object"),
        ({"version": 3, "tools": []}, "tools must be an object"),
        (
            {"version": 3, "tools": {"maxParallelCalls": 0}},
            "tools.maxParallelCalls must be a positive integer",
        ),
        (
            {"version": 3, "tools": {"toolSearchMode": "deferred"}},
            "tools.toolSearchMode must be dispatcher or native",
        ),
        (
            {"version": 3, "subagents": {"enabled": "yes"}},
            "subagents.enabled must be a boolean",
        ),
        (
            {"version": 3, "subagents": {"timeoutSeconds": 0}},
            "subagents.timeoutSeconds must be a positive number",
        ),
        (
            {"version": 3, "subagents": {"maxTokens": 0}},
            "subagents.maxTokens must be a positive integer",
        ),
        (
            {"version": 3, "backgroundTasks": {"enabled": "yes"}},
            "backgroundTasks.enabled must be a boolean",
        ),
        ({"version": 3, "mcp": []}, "mcp must be an object"),
        (
            {"version": 3, "mcp": {"deferredToolThreshold": 0}},
            "Unknown setting mcp.deferredToolThreshold",
        ),
        (
            {
                "version": 3,
                "mcp": {"servers": {"Bad Name": {"command": "server"}}},
            },
            "MCP server name must match",
        ),
        (
            {
                "version": 3,
                "mcp": {
                    "servers": {
                        "server": {"command": "server", "envFrom": {"TOKEN": 1}}
                    }
                },
            },
            "envFrom must map environment names",
        ),
        (
            {
                "version": 3,
                "mcp": {
                    "servers": {
                        "server": {"command": "server", "env": {"TOKEN": "secret"}}
                    }
                },
            },
            "env cannot contain literal values",
        ),
    ],
)
def test_invalid_settings_are_rejected(
    tmp_path: Path, document: object, message: str
) -> None:
    paths = make_paths(tmp_path)
    paths.user_settings_path.parent.mkdir()
    paths.user_settings_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(SettingsFileError, match=message):
        SettingsStore(paths).load()


def test_shared_project_cannot_enable_bypass(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    paths.project_settings_path.parent.mkdir()
    paths.project_settings_path.write_text(
        json.dumps({"version": 3, "permissions": {"defaultMode": "bypassPermissions"}}),
        encoding="utf-8",
    )

    with pytest.raises(SettingsFileError, match="cannot enable bypassPermissions"):
        SettingsStore(paths).load()


def test_permission_rule_arrays_are_parsed_and_serialized(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    store = SettingsStore(paths)
    store.write(
        SettingsScope.USER,
        SettingsLayer(
            permission_allow_rules=("Bash(git status)", "Read"),
            permission_deny_rules=("Bash(rm:*)",),
            permission_ask_rules=("Bash(git push)",),
        ),
    )

    document = json.loads(paths.user_settings_path.read_text(encoding="utf-8"))
    assert document["permissions"] == {
        "allow": ["Bash(git status)", "Read"],
        "deny": ["Bash(rm:*)"],
        "ask": ["Bash(git push)"],
    }
    assert store.load_scope(SettingsScope.USER) == SettingsLayer(
        permission_allow_rules=("Bash(git status)", "Read"),
        permission_deny_rules=("Bash(rm:*)",),
        permission_ask_rules=("Bash(git push)",),
    )


@pytest.mark.parametrize(
    "document, message",
    [
        (
            {"version": 3, "permissions": {"ask": ["Bash(git push"]}},
            "Malformed",
        ),
        ({"version": 3, "permissions": {"allow": [42]}}, "must be a string"),
        (
            {"version": 3, "permissions": {"deny": "Bash(rm:*)"}},
            "must be an array",
        ),
    ],
)
def test_invalid_permission_rules_are_rejected(
    tmp_path: Path, document: object, message: str
) -> None:
    paths = make_paths(tmp_path)
    paths.user_settings_path.parent.mkdir()
    paths.user_settings_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(SettingsFileError, match=message):
        SettingsStore(paths).load()


def test_unknown_and_non_bash_content_rules_are_preserved(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    paths.user_settings_path.parent.mkdir()
    paths.user_settings_path.write_text(
        json.dumps(
            {
                "version": 3,
                "permissions": {
                    "allow": ["Missing(command)"],
                    "deny": ["Read(README.md)"],
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = SettingsStore(paths).load()

    assert loaded.permission_allow_rules == ("Missing(command)",)
    assert loaded.permission_deny_rules == ("Read(README.md)",)


def test_permission_rules_union_across_scopes_without_duplicates(
    tmp_path: Path,
) -> None:
    paths = make_paths(tmp_path)
    store = SettingsStore(paths)
    store.write(
        SettingsScope.USER,
        SettingsLayer(permission_allow_rules=("Bash(git status)", "Read")),
    )
    store.write(
        SettingsScope.PROJECT,
        SettingsLayer(permission_allow_rules=("Bash(git status)", "Bash(git diff)")),
    )
    store.write(
        SettingsScope.LOCAL,
        SettingsLayer(permission_allow_rules=("Bash(git diff)", "Read")),
    )

    assert store.load().permission_allow_rules == (
        "Bash(git status)",
        "Read",
        "Bash(git diff)",
    )


def test_write_merges_known_fields_and_preserves_unknown_keys(
    tmp_path: Path,
) -> None:
    paths = make_paths(tmp_path)
    store = SettingsStore(paths)
    store.write(
        SettingsScope.USER,
        SettingsLayer(
            model="first-model",
            permission_allow_rules=("Bash(git status)",),
        ),
    )
    document = json.loads(paths.user_settings_path.read_text(encoding="utf-8"))
    document["futureSetting"] = {"enabled": True}
    paths.user_settings_path.write_text(json.dumps(document), encoding="utf-8")

    store.write(
        SettingsScope.USER,
        SettingsLayer(
            model="second-model",
            permission_deny_rules=("Bash(rm:*)",),
        ),
    )

    document = json.loads(paths.user_settings_path.read_text(encoding="utf-8"))
    assert document["futureSetting"] == {"enabled": True}
    assert document["agent"]["model"] == "second-model"
    assert document["permissions"] == {
        "allow": ["Bash(git status)"],
        "deny": ["Bash(rm:*)"],
    }


def test_resolver_records_highest_priority_source_for_duplicate_rules(
    tmp_path: Path,
) -> None:
    paths = make_paths(tmp_path)
    store = SettingsStore(paths)
    store.write(
        SettingsScope.USER,
        SettingsLayer(permission_allow_rules=("Bash(git status)",)),
    )
    store.write(
        SettingsScope.PROJECT,
        SettingsLayer(permission_allow_rules=("Bash(git status)",)),
    )
    store.write(
        SettingsScope.LOCAL,
        SettingsLayer(permission_allow_rules=("Bash(git status)",)),
    )

    settings = SettingsResolver(paths).resolve(interactive=False)

    assert settings.permission_rules == (
        PermissionRule(
            "Bash",
            PermissionBehavior.ALLOW,
            "git status",
            source="localSettings",
        ),
    )


@pytest.mark.parametrize("scope", [SettingsScope.PROJECT, SettingsScope.LOCAL])
def test_project_scoped_settings_cannot_redirect_provider(
    tmp_path: Path, scope: SettingsScope
) -> None:
    paths = make_paths(tmp_path)
    path = paths.settings_path(scope)
    path.parent.mkdir(exist_ok=True)
    path.write_text(
        json.dumps({"version": 3, "baseUrl": "https://attacker.example"}),
        encoding="utf-8",
    )

    with pytest.raises(SettingsFileError, match="only allowed in user storage"):
        SettingsStore(paths).load_scope(scope)

    with pytest.raises(SettingsFileError, match="only allowed in user settings"):
        SettingsStore(paths).write(scope, SettingsLayer(active_provider="attacker"))


@pytest.mark.parametrize(
    "base_url",
    ["not-a-url", "ftp://example.com", "https://user:pass@example.com"],
)
def test_removed_user_base_url_setting_is_ignored(
    tmp_path: Path, base_url: str
) -> None:
    paths = make_paths(tmp_path)
    paths.user_settings_path.parent.mkdir()
    paths.user_settings_path.write_text(
        json.dumps({"version": 3, "baseUrl": base_url}), encoding="utf-8"
    )

    assert SettingsStore(paths).load() == SettingsLayer()


def test_write_is_atomic_and_private(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    store = SettingsStore(paths)

    store.write(
        SettingsScope.USER,
        SettingsLayer(model="model", context_chars=1234),
    )

    assert store.load_scope(SettingsScope.USER) == SettingsLayer(
        model="model", context_chars=1234
    )
    assert stat.S_IMODE(paths.user_settings_path.stat().st_mode) == 0o600
    assert list(paths.user_settings_path.parent.glob("*.tmp")) == []
