import ast
import inspect
from pathlib import Path

import nano_code.agent as agent_api
import nano_code.context as context_adapter
import nano_code.providers as provider_adapter
import nano_code.sessions as session_adapter
import nano_code.tools as tool_adapter
from nano_code.agent import (
    AgentEngine,
    AgentInboundPort,
)
from nano_code.application.chat.contracts import ChatRuntime, RuntimeStatus
from nano_code.context import (
    CompactionOutcome,
    ContextPlan,
)
from nano_code.context.attachments.models import ContextAttachment
from nano_code.conversation import Conversation, ConversationMessage, ToolResult
from nano_code.features.file_mentions import FileMention, PathSuggestion
from nano_code.features.todos import TodoItem, TodoWriteTool
from nano_code.model import ModelClient, ModelToolDefinition
from nano_code.permissions import PermissionPolicy, PermissionRequest
from nano_code.providers.anthropic import AnthropicProvider
from nano_code.providers.openai_responses import OpenAIResponsesProvider
from nano_code.providers.router import ProviderRouter
from nano_code.sessions import Session, SessionSnapshot
from nano_code.tools import ToolRoundCompleted, ToolUsePresentation
from nano_code.tools.round_executor import ToolRoundExecutor
from nano_code.workspace import Workspace

_AGENT_ROOT = Path(__file__).parents[2] / "src" / "nano_code" / "agent"
_PACKAGE_ROOT = _AGENT_ROOT.parent
_ADAPTER_PREFIXES = (
    "nano_code.context.planner",
    "nano_code.context.compaction",
    "nano_code.context.normalization",
    "nano_code.providers",
    "nano_code.tools.executor",
    "nano_code.tools.round_executor",
)


def test_model_exposes_the_single_authoritative_client_protocol() -> None:
    assert not hasattr(context_adapter, "ContextPort")
    assert ContextPlan.__module__ == "nano_code.context.models"
    assert not hasattr(agent_api, "ModelCallPort")
    assert not hasattr(agent_api, "ModelCompletionPort")
    assert not hasattr(provider_adapter, "ModelClient")
    assert ModelClient.__module__ == "nano_code.model.client"
    model_sources = tuple((_PACKAGE_ROOT / "model").glob("*.py"))
    protocol_declarations = [
        node
        for source_path in model_sources
        for node in ast.walk(ast.parse(source_path.read_text(encoding="utf-8")))
        if isinstance(node, ast.ClassDef)
        and any(
            isinstance(base, ast.Name) and base.id == "Protocol" for base in node.bases
        )
    ]
    assert [node.name for node in protocol_declarations] == ["ModelClient"]
    assert not hasattr(session_adapter, "SessionRepository")
    assert not hasattr(agent_api, "ConversationState")
    assert not hasattr(agent_api, "ToolRoundPort")
    assert tool_adapter.ToolRoundExecutor is ToolRoundExecutor
    assert ToolRoundCompleted.__module__ == "nano_code.tools.round_executor"
    assert not (_AGENT_ROOT / "contracts" / "tool.py").exists()
    assert not (_AGENT_ROOT / "ports" / "tool.py").exists()
    assert AgentInboundPort in AgentEngine.__mro__
    assert not (_AGENT_ROOT / "ports" / "context.py").exists()
    assert not (_AGENT_ROOT / "ports" / "compaction.py").exists()
    assert not (_AGENT_ROOT / "contracts" / "context.py").exists()
    assert not (_AGENT_ROOT / "contracts" / "compaction.py").exists()


def test_concrete_adapters_explicitly_inherit_their_real_protocols() -> None:
    adapters = (
        (ProviderRouter, (ModelClient,)),
        (AnthropicProvider, (ModelClient,)),
        (OpenAIResponsesProvider, (ModelClient,)),
        (AgentEngine, (AgentInboundPort,)),
    )
    for adapter, ports in adapters:
        assert all(port in adapter.__bases__ for port in ports), adapter


def test_agent_core_does_not_import_concrete_adapters() -> None:
    for source_path in _AGENT_ROOT.glob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported = (node.module,)
            else:
                continue
            assert not any(name.startswith(_ADAPTER_PREFIXES) for name in imported), (
                source_path
            )


def test_conversation_layer_dependency_boundaries() -> None:
    provider_sources = tuple((_PACKAGE_ROOT / "providers").glob("*.py"))
    for source_path in provider_sources:
        source = source_path.read_text(encoding="utf-8")
        assert "ConversationMessage" not in source
        assert "sessions.records" not in source
        assert "ContextPlan" not in source

    for source_path in _AGENT_ROOT.rglob("*.py"):
        source = source_path.read_text(encoding="utf-8")
        assert "from anthropic" not in source
        assert "import anthropic" not in source

    simultaneous = []
    for source_path in (_PACKAGE_ROOT / "sessions").glob("*.py"):
        source = source_path.read_text(encoding="utf-8")
        if "sessions.records" in source and "ConversationMessage" in source:
            simultaneous.append(source_path.name)
    assert set(simultaneous) == {"codec.py", "store.py"}

    assert "presentation" not in ToolResult.__dataclass_fields__
    assert Conversation.__module__ == "nano_code.conversation.state"
    for source_path in (_PACKAGE_ROOT / "conversation").glob("*.py"):
        imports = _imported_modules(source_path)
        assert not any(
            name.startswith(
                (
                    "nano_code.agent",
                    "nano_code.application",
                    "nano_code.context",
                    "nano_code.sessions",
                    "nano_code.tui",
                )
            )
            for name in imports
        ), source_path


def test_tool_permission_and_workspace_ownership() -> None:
    parameters = tuple(inspect.signature(PermissionPolicy.decide).parameters)
    assert parameters == ("self", "request")
    assert PermissionRequest.__module__ == "nano_code.permissions.models"
    assert Workspace.__module__ == "nano_code.workspace.local"

    for package_name, forbidden in (
        ("permissions", "nano_code.tools"),
        ("workspace", "nano_code.permissions"),
    ):
        for source_path in (_PACKAGE_ROOT / package_name).rglob("*.py"):
            assert not any(
                name == forbidden or name.startswith(f"{forbidden}.")
                for name in _imported_modules(source_path)
            ), source_path

    executor = (_PACKAGE_ROOT / "tools" / "executor.py").read_text(encoding="utf-8")
    assert "tool.check_permissions(" in executor
    assert "self.policy.decide(" in executor
    assert "await tool.execute(" in executor


def test_contracts_expose_one_authoritative_shape_without_legacy_aliases() -> None:
    assert ModelToolDefinition.__module__ == "nano_code.model.request"
    assert not (_AGENT_ROOT / "contracts" / "model.py").exists()
    assert not (_AGENT_ROOT / "ports" / "model.py").exists()
    assert not (_AGENT_ROOT / "contracts" / "tool_definition.py").exists()

    assert not hasattr(SessionSnapshot, "full_history")
    assert not hasattr(SessionSnapshot, "all_messages")
    assert not hasattr(SessionSnapshot, "messages")
    assert not hasattr(SessionSnapshot, "working_messages")
    assert not hasattr(SessionSnapshot, "replacements")
    assert not hasattr(SessionSnapshot, "boundaries")
    assert not hasattr(SessionSnapshot, "compact_boundary")

    assert not hasattr(CompactionOutcome, "summary_message")
    assert not hasattr(CompactionOutcome, "content_replacements")
    assert not hasattr(CompactionOutcome, "summary_text")
    assert "results" not in ToolRoundCompleted.__dataclass_fields__
    assert not hasattr(AgentEngine, "submit_stream")
    assert not hasattr(AgentEngine, "state")
    assert not hasattr(AgentEngine, "context_state")
    assert not hasattr(AgentEngine, "working_messages")
    assert not hasattr(AgentInboundPort, "session_id")
    assert not hasattr(AgentInboundPort, "message_count")
    assert not hasattr(agent_api, "AgentState")
    assert not hasattr(agent_api, "AgentContextState")
    assert not hasattr(Conversation, "messages")
    assert not (_AGENT_ROOT / "contracts" / "session.py").exists()
    assert not (_AGENT_ROOT / "ports" / "session.py").exists()
    assert Session.__module__ == "nano_code.sessions.session"
    assert TodoWriteTool().definition.name == "TodoWrite"


def test_core_bootstrap_is_the_only_full_application_composition_root() -> None:
    assert not tuple((_PACKAGE_ROOT / "config").glob("*.py"))

    bootstrap = (_PACKAGE_ROOT / "core" / "bootstrap.py").read_text(encoding="utf-8")
    for dependency in (
        "ContextBuilder",
        "ProviderRouter",
        "SessionStore",
        "ToolExecutor",
        "AgentEngine",
    ):
        assert dependency in bootstrap

    chat_runtime = (_PACKAGE_ROOT / "application" / "chat" / "runtime.py").read_text(
        encoding="utf-8"
    )
    for concrete in (
        "ContextBuilder",
        "ToolExecutor",
        "SessionStore",
        "ProviderRouter",
        "AgentEngine",
    ):
        assert concrete not in chat_runtime

    cli_arguments = (_PACKAGE_ROOT / "cli" / "arguments.py").read_text(encoding="utf-8")
    for settings_dependency in (
        "SettingsStore",
        "CredentialStore",
        "ProviderProfileStore",
        "resolve_api_key",
    ):
        assert settings_dependency not in cli_arguments


def _imported_modules(source_path: Path) -> tuple[str, ...]:
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.append(node.module)
    return tuple(modules)


def test_application_chat_owns_frontend_neutral_contracts() -> None:
    assert ChatRuntime.__module__ == "nano_code.application.chat.contracts"
    assert RuntimeStatus.__module__ == "nano_code.application.chat.contracts"
    assert ToolUsePresentation.__module__ == "nano_code.tools.presentation"

    for source_path in (_PACKAGE_ROOT / "application" / "chat").glob("*.py"):
        imports = _imported_modules(source_path)
        assert not any(
            name == "nano_code.tui" or name.startswith("nano_code.tui.")
            for name in imports
        ), source_path
        assert not any(
            name == "nano_code.cli" or name.startswith("nano_code.cli.")
            for name in imports
        ), source_path


def test_production_code_does_not_depend_on_legacy_chat_owners() -> None:
    legacy_modules = {
        _PACKAGE_ROOT / "cli" / "runtime.py",
        _PACKAGE_ROOT / "presentation.py",
        _PACKAGE_ROOT / "tui" / "contracts.py",
    }
    assert not any(path.exists() for path in legacy_modules)
    forbidden = (
        "nano_code.cli.runtime",
        "nano_code.presentation",
        "nano_code.tui.contracts",
    )
    for source_path in _PACKAGE_ROOT.rglob("*.py"):
        imports = _imported_modules(source_path)
        assert not any(name.startswith(forbidden) for name in imports), source_path

    tui_root_imports = _imported_modules(_PACKAGE_ROOT / "tui" / "__init__.py")
    assert not any(
        name == "nano_code.application.chat.contracts"
        or name.startswith("nano_code.application.chat.contracts.")
        for name in tui_root_imports
    )


def test_core_mechanisms_do_not_import_chat_runtime() -> None:
    for package_name in ("agent", "context", "conversation", "sessions", "tools"):
        for source_path in (_PACKAGE_ROOT / package_name).rglob("*.py"):
            imports = _imported_modules(source_path)
            assert "nano_code.application.chat.runtime" not in imports, source_path
            assert "nano_code.application.chat.permissions" not in imports, source_path


def test_file_mentions_are_a_feature_not_a_top_level_or_tui_domain() -> None:
    assert FileMention.__module__ == "nano_code.features.file_mentions.models"
    assert PathSuggestion.__module__ == "nano_code.features.file_mentions.models"
    assert not (_PACKAGE_ROOT / "attachments.py").exists()

    feature_root = _PACKAGE_ROOT / "features" / "file_mentions"
    for source_path in feature_root.glob("*.py"):
        imports = _imported_modules(source_path)
        assert not any(
            name == "nano_code.tui" or name.startswith("nano_code.tui.")
            for name in imports
        ), source_path
        assert not any(
            name == "nano_code.cli" or name.startswith("nano_code.cli.")
            for name in imports
        ), source_path

    loader = (feature_root / "loader.py").read_text(encoding="utf-8")
    assert "ToolExecutor" not in loader
    assert "ToolCall" not in loader
    assert "ToolInvocation" not in loader
    assert "tool.execute(" not in loader
    assert "policy.decide" not in loader

    completion = (_PACKAGE_ROOT / "tui" / "completion.py").read_text(encoding="utf-8")
    assert "features.file_mentions" not in completion


def test_conversation_and_context_attachment_ownership() -> None:
    assert ConversationMessage.__module__ == "nano_code.conversation.models"
    assert ContextAttachment.__module__ == "nano_code.context.attachments.models"
    assert not tuple((_PACKAGE_ROOT / "messages").glob("*.py"))
    assert not (_PACKAGE_ROOT / "context" / "attachment_projection.py").exists()
    assert not (_PACKAGE_ROOT / "context" / "attachments.py").exists()

    conversation_root = _PACKAGE_ROOT / "conversation"
    for source_path in conversation_root.glob("*.py"):
        imports = _imported_modules(source_path)
        assert not any(
            name == "nano_code.context" or name.startswith("nano_code.context.")
            for name in imports
        ), source_path

    attachment_models = _PACKAGE_ROOT / "context" / "attachments" / "models.py"
    assert not any(
        name == "nano_code.agent" or name.startswith("nano_code.agent.")
        for name in _imported_modules(attachment_models)
    )

    projection = (
        _PACKAGE_ROOT / "context" / "attachments" / "projection.py"
    ).read_text(encoding="utf-8")
    assert "ModelAssistantMessage" not in projection
    assert "ModelToolUseBlock" not in projection
    assert "ModelToolResultBlock" not in projection

    for source_path in (_PACKAGE_ROOT / "providers").glob("*.py"):
        imports = _imported_modules(source_path)
        assert "nano_code.context.attachments.models" not in imports, source_path


def test_todos_are_a_self_contained_product_feature() -> None:
    assert TodoItem.__module__ == "nano_code.features.todos.models"
    assert not tuple((_PACKAGE_ROOT / "todos").glob("*.py"))

    feature_root = _PACKAGE_ROOT / "features" / "todos"
    assert {path.name for path in feature_root.glob("*.py")} == {
        "__init__.py",
        "codec.py",
        "models.py",
        "projection.py",
        "reminder.py",
        "tool.py",
    }
    for source_path in feature_root.glob("*.py"):
        imports = _imported_modules(source_path)
        assert not any(
            name == "nano_code.tui" or name.startswith("nano_code.tui.")
            for name in imports
        ), source_path
        assert not any(
            name == "nano_code.cli" or name.startswith("nano_code.cli.")
            for name in imports
        ), source_path

    assert not (_PACKAGE_ROOT / "tools" / "builtin" / "todo_write.py").exists()
    for source_path in (_PACKAGE_ROOT / "tools").rglob("*.py"):
        assert not any(
            name == "nano_code.features.todos"
            or name.startswith("nano_code.features.todos.")
            for name in _imported_modules(source_path)
        ), source_path


def test_context_and_base_modules_do_not_import_concrete_features() -> None:
    for source_path in (_PACKAGE_ROOT / "context").rglob("*.py"):
        assert not any(
            name == "nano_code.agent" or name.startswith("nano_code.agent.")
            for name in _imported_modules(source_path)
        ), source_path

    for package_name in (
        "agent",
        "context",
        "conversation",
        "model",
        "permissions",
        "sessions",
        "tools",
        "workspace",
    ):
        for source_path in (_PACKAGE_ROOT / package_name).rglob("*.py"):
            assert not any(
                name == "nano_code.features" or name.startswith("nano_code.features.")
                for name in _imported_modules(source_path)
            ), source_path
