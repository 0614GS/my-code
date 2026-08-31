import ast
import inspect
from pathlib import Path

import my_code.agent as agent_api
import my_code.context as context_adapter
import my_code.providers as provider_adapter
import my_code.sessions as session_adapter
import my_code.sessions.models as session_models
import my_code.tools as tool_adapter
from my_code.agent.engine import AgentEngine
from my_code.chat.service import ChatService
from my_code.chat.status import RuntimeStatus
from my_code.context.engine import ContextEngine
from my_code.context.models import CompactionOutcome, ContextPlan
from my_code.conversation.models import AttachmentMessage, ConversationEntry, ToolResult
from my_code.features.file_mentions.models import FileMention, PathSuggestion
from my_code.features.todos.models import TodoItem
from my_code.features.todos.tool import TodoWriteTool
from my_code.model.client import ModelClient
from my_code.model.request import ModelToolDefinition
from my_code.permissions.models import PermissionRequest
from my_code.permissions.policy import PermissionPolicy
from my_code.providers.anthropic import AnthropicProvider
from my_code.providers.openai_responses import OpenAIResponsesProvider
from my_code.providers.router import ProviderRouter
from my_code.sessions.session import Session
from my_code.tools.presentation import ToolUsePresentation
from my_code.tools.round_executor import ToolRoundCompleted, ToolRoundExecutor
from my_code.workspace.local import Workspace

_AGENT_ROOT = Path(__file__).parents[2] / "src" / "my_code" / "agent"
_PACKAGE_ROOT = _AGENT_ROOT.parent
_ADAPTER_PREFIXES = (
    "my_code.providers",
    "my_code.chat",
    "my_code.cli",
    "my_code.tui",
    "my_code.bootstrap",
)


def test_model_exposes_the_single_authoritative_client_protocol() -> None:
    assert not hasattr(context_adapter, "ContextPort")
    assert ContextPlan.__module__ == "my_code.context.models"
    assert not hasattr(agent_api, "ModelCallPort")
    assert not hasattr(agent_api, "ModelCompletionPort")
    assert not hasattr(provider_adapter, "ModelClient")
    assert ModelClient.__module__ == "my_code.model.client"
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
    assert not hasattr(tool_adapter, "ToolRoundExecutor")
    assert ToolRoundExecutor.__module__ == "my_code.tools.round_executor"
    assert ToolRoundCompleted.__module__ == "my_code.tools.round_executor"
    assert not (_AGENT_ROOT / "contracts" / "tool.py").exists()
    assert not (_AGENT_ROOT / "ports" / "tool.py").exists()
    assert not hasattr(agent_api, "AgentInboundPort")
    assert AgentEngine.__bases__ == (object,)
    assert not (_AGENT_ROOT / "ports").exists()
    assert not (_AGENT_ROOT / "contracts").exists()
    assert not (_AGENT_ROOT / "ports" / "context.py").exists()
    assert not (_AGENT_ROOT / "ports" / "compaction.py").exists()
    assert not (_AGENT_ROOT / "contracts" / "context.py").exists()
    assert not (_AGENT_ROOT / "contracts" / "compaction.py").exists()


def test_concrete_adapters_explicitly_inherit_their_real_protocols() -> None:
    adapters = (
        (ProviderRouter, (ModelClient,)),
        (AnthropicProvider, (ModelClient,)),
        (OpenAIResponsesProvider, (ModelClient,)),
    )
    for adapter, ports in adapters:
        assert all(port in adapter.__bases__ for port in ports), adapter


def test_agent_core_does_not_import_hosts_or_provider_implementations() -> None:
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
        assert "sessions._records" not in source
        assert "ContextPlan" not in source

    for source_path in _AGENT_ROOT.rglob("*.py"):
        source = source_path.read_text(encoding="utf-8")
        assert "from anthropic" not in source
        assert "import anthropic" not in source

    simultaneous = []
    for source_path in (_PACKAGE_ROOT / "sessions").glob("*.py"):
        source = source_path.read_text(encoding="utf-8")
        if "sessions._records" in source and "ConversationEntry" in source:
            simultaneous.append(source_path.name)
    assert set(simultaneous) == {"_codec.py", "_store.py"}

    assert "presentation" in ToolResult.__dataclass_fields__
    for source_path in (_PACKAGE_ROOT / "providers").glob("*.py"):
        assert "ToolResultPresentation" not in source_path.read_text(encoding="utf-8")
    for source_path in (_PACKAGE_ROOT / "conversation").glob("*.py"):
        imports = _imported_modules(source_path)
        assert not any(
            name.startswith(
                (
                    "my_code.agent",
                    "my_code.application",
                    "my_code.context",
                    "my_code.sessions",
                    "my_code.tui",
                )
            )
            for name in imports
        ), source_path


def test_tool_permission_and_workspace_ownership() -> None:
    parameters = tuple(inspect.signature(PermissionPolicy.decide).parameters)
    assert parameters == ("self", "request")
    assert PermissionRequest.__module__ == "my_code.permissions.models"
    assert Workspace.__module__ == "my_code.workspace.local"

    for package_name, forbidden in (
        ("permissions", "my_code.tools"),
        ("workspace", "my_code.permissions"),
    ):
        for source_path in (_PACKAGE_ROOT / package_name).rglob("*.py"):
            assert not any(
                name == forbidden or name.startswith(f"{forbidden}.")
                for name in _imported_modules(source_path)
            ), source_path

    executor = (_PACKAGE_ROOT / "tools" / "executor.py").read_text(encoding="utf-8")
    assert "tool.check_permissions(" in executor
    assert "active_policy.decide(" in executor
    assert "await tool.execute(" in executor


def test_contracts_expose_one_authoritative_shape_without_legacy_aliases() -> None:
    assert ModelToolDefinition.__module__ == "my_code.model.request"
    assert not (_AGENT_ROOT / "contracts" / "model.py").exists()
    assert not (_AGENT_ROOT / "ports" / "model.py").exists()
    assert not (_AGENT_ROOT / "contracts" / "tool_definition.py").exists()

    assert not hasattr(session_models, "SessionSnapshot")
    assert not hasattr(Session, "snapshot")
    assert not hasattr(Session, "context_snapshot")
    assert not hasattr(Session, "tool_presentation")

    assert not hasattr(CompactionOutcome, "summary_message")
    assert not hasattr(CompactionOutcome, "content_replacements")
    assert not hasattr(CompactionOutcome, "summary_text")
    assert "results" not in ToolRoundCompleted.__dataclass_fields__
    assert not hasattr(AgentEngine, "submit_stream")
    assert not hasattr(AgentEngine, "state")
    assert not hasattr(AgentEngine, "context_state")
    assert not hasattr(AgentEngine, "working_messages")
    assert not hasattr(AgentEngine, "compact")
    assert not hasattr(AgentEngine, "inspect")
    assert not hasattr(AgentEngine, "present_use")
    assert not hasattr(AgentEngine, "present_stored_result")
    assert not hasattr(agent_api, "AgentState")
    assert not hasattr(agent_api, "AgentContextState")
    assert not (_AGENT_ROOT / "contracts" / "session.py").exists()
    assert not (_AGENT_ROOT / "ports" / "session.py").exists()
    assert Session.__module__ == "my_code.sessions.session"
    assert TodoWriteTool().definition.name == "TodoWrite"


def test_root_bootstrap_is_the_only_full_application_composition_root() -> None:
    assert tuple((_PACKAGE_ROOT / "config").glob("*.py"))
    assert not (_PACKAGE_ROOT / "core").exists()

    bootstrap = (_PACKAGE_ROOT / "bootstrap.py").read_text(encoding="utf-8")
    for dependency in (
        "ContextEngine",
        "ContextPlanner",
        "ContextCompactor",
        "ProviderRouter",
        "Session",
        "ToolExecutor",
        "AgentEngine",
    ):
        assert dependency in bootstrap

    chat_runtime = (_PACKAGE_ROOT / "chat" / "service.py").read_text(encoding="utf-8")
    assert "ContextEngine" in chat_runtime
    assert "ToolExecutor" in chat_runtime
    assert "ContextPlanner" not in chat_runtime
    assert "ContextCompactor" not in chat_runtime
    assert ContextEngine.__module__ == "my_code.context.engine"

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


def test_chat_owns_frontend_neutral_contracts_without_runtime_protocol() -> None:
    assert ChatService.__module__ == "my_code.chat.service"
    assert RuntimeStatus.__module__ == "my_code.chat.status"
    assert not hasattr(
        __import__("my_code.chat", fromlist=["ChatRuntime"]), "ChatRuntime"
    )
    assert ToolUsePresentation.__module__ == "my_code.tools.presentation"

    for source_path in (_PACKAGE_ROOT / "chat").glob("*.py"):
        imports = _imported_modules(source_path)
        assert not any(
            name == "my_code.tui" or name.startswith("my_code.tui.") for name in imports
        ), source_path
        assert not any(
            name == "my_code.cli" or name.startswith("my_code.cli.") for name in imports
        ), source_path


def test_production_code_does_not_depend_on_legacy_chat_owners() -> None:
    legacy_modules = {
        _PACKAGE_ROOT / "cli" / "runtime.py",
        _PACKAGE_ROOT / "presentation.py",
        _PACKAGE_ROOT / "tui" / "contracts.py",
    }
    assert not any(path.exists() for path in legacy_modules)
    forbidden = (
        "my_code.cli.runtime",
        "my_code.presentation",
        "my_code.tui.contracts",
    )
    for source_path in _PACKAGE_ROOT.rglob("*.py"):
        imports = _imported_modules(source_path)
        assert not any(name.startswith(forbidden) for name in imports), source_path

    assert (_PACKAGE_ROOT / "runtime" / "state.py").exists()


def test_core_mechanisms_do_not_import_chat_service() -> None:
    for package_name in ("agent", "context", "conversation", "sessions", "tools"):
        for source_path in (_PACKAGE_ROOT / package_name).rglob("*.py"):
            imports = _imported_modules(source_path)
            assert not any(name.startswith("my_code.chat") for name in imports), (
                source_path
            )


def test_file_mentions_are_a_feature_not_a_top_level_or_tui_domain() -> None:
    assert FileMention.__module__ == "my_code.features.file_mentions.models"
    assert PathSuggestion.__module__ == "my_code.features.file_mentions.models"
    assert not (_PACKAGE_ROOT / "attachments.py").exists()

    feature_root = _PACKAGE_ROOT / "features" / "file_mentions"
    for source_path in feature_root.glob("*.py"):
        imports = _imported_modules(source_path)
        assert not any(
            name == "my_code.tui" or name.startswith("my_code.tui.") for name in imports
        ), source_path
        assert not any(
            name == "my_code.cli" or name.startswith("my_code.cli.") for name in imports
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
    assert "HumanMessage" in str(ConversationEntry.__value__)
    assert "AttachmentMessage" in str(ConversationEntry.__value__)
    assert AttachmentMessage.__module__ == "my_code.conversation.models"
    assert not (_PACKAGE_ROOT / "context" / "attachments" / "models.py").exists()
    assert not tuple((_PACKAGE_ROOT / "messages").glob("*.py"))
    assert not (_PACKAGE_ROOT / "context" / "attachment_projection.py").exists()
    assert not (_PACKAGE_ROOT / "context" / "attachments.py").exists()

    conversation_root = _PACKAGE_ROOT / "conversation"
    for source_path in conversation_root.glob("*.py"):
        imports = _imported_modules(source_path)
        assert not any(
            name == "my_code.context" or name.startswith("my_code.context.")
            for name in imports
        ), source_path

    projection = (
        _PACKAGE_ROOT / "context" / "attachments" / "projection.py"
    ).read_text(encoding="utf-8")
    assert "ModelAssistantMessage" not in projection
    assert "ModelToolUseBlock" not in projection
    assert "ModelToolResultBlock" not in projection

    for source_path in (_PACKAGE_ROOT / "providers").glob("*.py"):
        imports = _imported_modules(source_path)
        assert not any(
            name.startswith("my_code.conversation.attachments") for name in imports
        ), source_path


def test_session_is_the_only_public_conversation_and_persistence_boundary() -> None:
    assert isinstance(Session.conversation, property)
    assert isinstance(Session.context_entries, property)
    assert not hasattr(Session, "store")
    assert not hasattr(Session, "append")
    for source_path in _PACKAGE_ROOT.rglob("*.py"):
        if "sessions" in source_path.relative_to(_PACKAGE_ROOT).parts:
            continue
        imports = _imported_modules(source_path)
        assert "my_code.sessions._store" not in imports, source_path
        assert "my_code.sessions._codec" not in imports, source_path
        assert "my_code.sessions._records" not in imports, source_path


def test_full_app_state_stays_at_the_application_boundary() -> None:
    allowed = {
        Path("application/state.py"),
        Path("bootstrap.py"),
        Path("chat/service.py"),
    }
    for source_path in _PACKAGE_ROOT.rglob("*.py"):
        relative_path = source_path.relative_to(_PACKAGE_ROOT)
        if relative_path in allowed:
            continue
        imports = _imported_modules(source_path)
        assert not any(
            name == "my_code.application.state"
            or name.startswith("my_code.application.state.")
            for name in imports
        ), source_path


def test_todos_are_a_self_contained_product_feature() -> None:
    assert TodoItem.__module__ == "my_code.features.todos.models"
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
            name == "my_code.tui" or name.startswith("my_code.tui.") for name in imports
        ), source_path
        assert not any(
            name == "my_code.cli" or name.startswith("my_code.cli.") for name in imports
        ), source_path

    assert not (_PACKAGE_ROOT / "tools" / "builtin" / "todo_write.py").exists()
    for source_path in (_PACKAGE_ROOT / "tools").rglob("*.py"):
        assert not any(
            name == "my_code.features.todos"
            or name.startswith("my_code.features.todos.")
            for name in _imported_modules(source_path)
        ), source_path


def test_context_and_base_modules_do_not_import_concrete_features() -> None:
    for source_path in (_PACKAGE_ROOT / "context").rglob("*.py"):
        assert not any(
            name == "my_code.agent" or name.startswith("my_code.agent.")
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
                name == "my_code.features" or name.startswith("my_code.features.")
                for name in _imported_modules(source_path)
            ), source_path
