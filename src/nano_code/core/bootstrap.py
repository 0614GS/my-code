"""应用存储初始化与 Agent 依赖图的唯一 composition root。"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from nano_code.agent import AgentEngine, ConversationState
from nano_code.agent.contracts.session import SessionStart
from nano_code.application.chat.permissions import DeferredPermissionPrompter
from nano_code.application.chat.runtime import DefaultChatRuntime
from nano_code.auth import CredentialStore
from nano_code.cli.services import CliProviderController, ProjectSessionSource
from nano_code.context.attachments.sources import DerivedAttachmentResolver
from nano_code.context.compaction import (
    CompactionCoordinator,
    CompactionService,
)
from nano_code.context.planner import ContextPlanner
from nano_code.context.user_context import AgentsUserContextResolver
from nano_code.context.window import ContextWindow
from nano_code.core.paths import NanoCodePaths, SettingsScope
from nano_code.core.settings import AgentSettings
from nano_code.core.settings_store import SettingsLayer, SettingsStore
from nano_code.features.file_mentions import (
    AttachmentLoader,
    WorkspaceAttachmentReader,
    WorkspacePathSuggester,
)
from nano_code.features.todos.reminder import TodoReminderAttachmentSource
from nano_code.permissions import PermissionPolicy
from nano_code.permissions.prompt import (
    HeadlessPrompter,
    PermissionPrompter,
    TerminalPrompter,
)
from nano_code.permissions.updates import PermissionUpdateApplier
from nano_code.prompts import build_system_prompt_registry
from nano_code.providers.manager import ProviderManager
from nano_code.providers.profiles import (
    DEFAULT_MODEL,
    DEFAULT_PROVIDER_ID,
    ProviderProfile,
    ProviderProfileStore,
    ProviderProtocol,
)
from nano_code.providers.router import ProviderConnection, ProviderRouter
from nano_code.sessions import SessionStore
from nano_code.tools import ToolContext, ToolRegistry
from nano_code.tools.builtin import builtin_tools
from nano_code.tools.executor import ToolExecutor
from nano_code.tools.result_store import ToolResultStore
from nano_code.tools.round_executor import ToolRoundExecutor


@dataclass(frozen=True, slots=True)
class StorageInitialization:
    created_settings: bool
    created_providers: bool
    created_credentials: bool


def initialize_user_storage(paths: NanoCodePaths) -> StorageInitialization:
    """创建运行所需用户文件，不触碰项目 settings 或 session。"""

    for directory in (paths.config_home, paths.projects_dir):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)

    settings_store = SettingsStore(paths)
    created_settings = not paths.user_settings_path.exists()
    if created_settings:
        user_settings = SettingsLayer(active_provider=DEFAULT_PROVIDER_ID)
        settings_store.write(SettingsScope.USER, user_settings)
    else:
        user_settings = settings_store.load_scope(SettingsScope.USER)

    default_profile = ProviderProfile(
        id=user_settings.active_provider or DEFAULT_PROVIDER_ID,
        model=DEFAULT_MODEL,
    )
    created_providers = ProviderProfileStore(paths.providers_path).ensure_exists(
        default_profile
    )
    created_credentials = CredentialStore(paths.credentials_path).ensure_exists()
    return StorageInitialization(
        created_settings=created_settings,
        created_providers=created_providers,
        created_credentials=created_credentials,
    )


def _assemble_agent(
    settings: AgentSettings,
    session_id: str | None = None,
    *,
    permission_prompter: PermissionPrompter | None = None,
) -> tuple[
    AgentEngine,
    ProviderRouter,
    ToolRegistry,
    PermissionPolicy,
    ToolContext,
    ToolExecutor,
]:
    actual_session_id = session_id or str(uuid4())
    repository = SessionStore(
        settings.paths.project_state_dir,
        actual_session_id,
        start=SessionStart(
            session_id=actual_session_id,
            created_at=datetime.now(UTC).isoformat(),
            cwd=str(settings.cwd),
            provider_id=settings.provider_id,
            model=settings.model,
            permission_mode=settings.permission_mode.value,
            max_steps=settings.max_steps,
            max_output_tokens=settings.max_output_tokens,
            context_chars=settings.context_chars,
        ),
    )
    conversation = ConversationState(repository)
    registry = ToolRegistry(builtin_tools())
    prompter = permission_prompter or (
        TerminalPrompter() if settings.interactive else HeadlessPrompter()
    )
    permission_policy = PermissionPolicy(
        mode=settings.permission_mode,
        rules=settings.permission_rules,
    )
    tool_context = ToolContext(cwd=settings.cwd)
    tool_executor = ToolExecutor(
        registry=registry,
        policy=permission_policy,
        prompter=prompter,
        context=tool_context,
        result_store=ToolResultStore(
            settings.paths.tool_results_dir(actual_session_id)
        ),
        update_applier=PermissionUpdateApplier(
            permission_policy, SettingsStore(settings.paths)
        ),
    )
    provider = ProviderRouter(
        ProviderConnection(
            id=settings.provider_id,
            protocol=ProviderProtocol.ANTHROPIC_MESSAGES,
            model=settings.model,
            base_url=settings.base_url,
            api_key=settings.api_key,
            credential_source=settings.credential_source,
        )
    )
    context = ContextPlanner(
        window=ContextWindow(settings.context_chars),
        prompt=build_system_prompt_registry(settings.cwd),
        tools=registry.definitions,
        max_output_tokens=settings.max_output_tokens,
        user_context_resolver=AgentsUserContextResolver(settings.cwd),
        attachment_resolver=DerivedAttachmentResolver(
            (TodoReminderAttachmentSource(),)
        ),
    )
    tool_round = ToolRoundExecutor(
        tool_executor,
        result_store_factory=lambda active_id: ToolResultStore(
            settings.paths.tool_results_dir(active_id)
        ),
    )
    engine = AgentEngine(
        model_call=provider,
        tool_round=tool_round,
        conversation=conversation,
        context=context,
        compactor=CompactionCoordinator(context, CompactionService(provider)),
        max_steps=settings.max_steps,
    )
    return engine, provider, registry, permission_policy, tool_context, tool_executor


def bootstrap_agent(
    settings: AgentSettings,
    session_id: str | None = None,
    *,
    permission_prompter: PermissionPrompter | None = None,
) -> AgentEngine:
    """组装一个可由任意 driving adapter 使用的 Agent。"""

    engine, _, _, _, _, _ = _assemble_agent(
        settings,
        session_id,
        permission_prompter=permission_prompter,
    )
    return engine


def bootstrap_cli_runtime(
    settings: AgentSettings,
    session_id: str | None = None,
) -> DefaultChatRuntime:
    """组装 TUI 所需的 Agent 与 CLI application adapter。"""

    prompter = DeferredPermissionPrompter()
    engine, provider, _, _, _, tool_executor = _assemble_agent(
        settings,
        session_id,
        permission_prompter=prompter,
    )
    return DefaultChatRuntime(
        agent=engine,
        settings=settings,
        permission_prompter=prompter,
        provider_control=CliProviderController(
            ProviderManager(settings.paths), provider
        ),
        session_source=ProjectSessionSource(settings.paths.project_state_dir),
        attachment_loader=AttachmentLoader(
            WorkspaceAttachmentReader(settings.cwd, tool_executor.policy)
        ),
        path_suggester=WorkspacePathSuggester(settings.cwd),
    )
