"""Application composition root and process entrypoint."""

import asyncio
import os
import sys
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from my_code.agent.budget import TokenBudgetModelClient
from my_code.agent.engine import AgentEngine
from my_code.auth.credentials import CredentialStore
from my_code.chat.events import MaxStepsReached, TurnSucceeded
from my_code.chat.permissions import DeferredPermissionPrompter
from my_code.chat.service import ChatService
from my_code.cli.arguments import AuthOptions, CliOptions, parse_cli
from my_code.cli.auth import run_auth_command
from my_code.config.paths import MyCodePaths, SettingsScope
from my_code.config.permission_updates import PermissionUpdateApplier
from my_code.config.providers import (
    DEFAULT_MODEL,
    DEFAULT_PROVIDER_ID,
    ProviderProfile,
    ProviderProfileStore,
)
from my_code.config.settings import AgentSettings, SettingsResolver
from my_code.config.store import SettingsLayer, SettingsStore
from my_code.context.attachments.sources import (
    DerivedAttachmentResolver,
    DerivedAttachmentSource,
)
from my_code.context.compaction import ContextCompactor
from my_code.context.engine import ContextEngine
from my_code.context.planner import ContextPlanner
from my_code.context.user_context import AgentsUserContextResolver
from my_code.context.window import ContextWindow
from my_code.features.file_mentions.loader import AttachmentLoader
from my_code.features.file_mentions.reader import WorkspaceAttachmentReader
from my_code.features.file_mentions.suggestions import WorkspacePathSuggester
from my_code.features.subagents.controller import SubagentController
from my_code.features.subagents.definitions import build_subagent_definitions
from my_code.features.subagents.models import SubagentLimits, SubagentParentContext
from my_code.features.subagents.notifications import BackgroundTaskNotificationSource
from my_code.features.subagents.task_tools import (
    TaskCancelTool,
    TaskListTool,
    TaskOutputTool,
)
from my_code.features.subagents.tool import SubagentTool
from my_code.features.todos.reminder import TodoReminderAttachmentSource
from my_code.features.todos.tool import TodoWriteTool
from my_code.mcp.models import McpServerScope, McpServerSpec
from my_code.mcp.runtime import McpRuntime
from my_code.mcp.stdio import StdioMcpTransportFactory
from my_code.mcp.transport import McpTransportFactory
from my_code.model.capabilities import ActiveModelEnvironment, resolve_environment
from my_code.model.client import ModelClient
from my_code.model.primitives import ProviderBinding
from my_code.permissions.models import PermissionPrompter
from my_code.permissions.policy import PermissionPolicy
from my_code.permissions.prompt import HeadlessPrompter, TerminalPrompter
from my_code.prompts.registry import PromptRegistry
from my_code.prompts.system import build_system_prompt_registry
from my_code.providers.discovery import ModelDiscoveryService, resolve_without_network
from my_code.providers.leases import ProviderClientLease, ProviderLeaseRegistry
from my_code.providers.manager import ProviderManager
from my_code.providers.model_cache import ModelCatalogCache
from my_code.providers.router import ProviderConnection, ProviderRouter
from my_code.runtime.runs import (
    AgentRunComponents,
    AgentRunFactory,
    AgentRunSpec,
)
from my_code.runtime.state import (
    AppState,
    PermissionState,
    ProviderRuntime,
    ToolState,
    WorkspaceState,
)
from my_code.sessions.models import SessionStart
from my_code.sessions.session import Session
from my_code.skills.attachments import SkillListingAttachmentSource
from my_code.skills.discovery import SkillSearchRoot
from my_code.skills.models import SkillSourceId, SkillSourceKind
from my_code.skills.runtime import SkillRuntime
from my_code.skills.tool import restore_skill_permissions
from my_code.tasks.supervisor import TaskSupervisor
from my_code.tools.base import Tool, ToolContext
from my_code.tools.builtin import builtin_tools
from my_code.tools.catalog import (
    ToolCatalog,
    ToolCatalogSnapshot,
    ToolSourceId,
)
from my_code.tools.executor import ToolExecutor
from my_code.tools.round_executor import ToolRoundExecutor
from my_code.tui.app import MyCodeTui
from my_code.workspace.local import Workspace


@dataclass(frozen=True, slots=True)
class StorageInitialization:
    created_settings: bool
    created_providers: bool
    created_credentials: bool


@dataclass(frozen=True, slots=True)
class ApplicationAssembly:
    """Concrete components assembled once by the composition root."""

    agent: AgentEngine
    context: ContextEngine
    provider: ProviderRouter
    tool_catalog: ToolCatalog
    initial_tools: ToolCatalogSnapshot
    permissions: PermissionPolicy
    tool_context: ToolContext
    tool_executor: ToolExecutor
    provider_runtime: ProviderRuntime
    run_factory: AgentRunFactory
    tasks: TaskSupervisor
    mcp: McpRuntime
    skills: SkillRuntime
    session: Session


async def discover_active_model(
    settings: AgentSettings, *, timeout_seconds: float = 3.0
) -> AgentSettings:
    """Best-effort startup discovery; failure remains observable and non-fatal."""

    profile = ProviderProfile(
        id=settings.provider_id,
        model=settings.model,
        protocol=settings.protocol,
        base_url=settings.base_url,
        reasoning=settings.reasoning,
        limits=settings.model_limits,
        compact=settings.compact,
    )
    descriptor, discovered_at, error = await ModelDiscoveryService(
        ModelCatalogCache(settings.paths.model_cache_path)
    ).resolve(
        profile,
        api_key=settings.api_key,
        timeout_seconds=timeout_seconds,
    )
    return replace(
        settings,
        model_limits=descriptor.limits,
        model_descriptor=descriptor,
        model_discovered_at=discovered_at,
        model_discovery_error=error,
    )


def initialize_user_storage(paths: MyCodePaths) -> StorageInitialization:
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


def _build_agent_components(
    settings: AgentSettings,
    *,
    model_call: ModelClient,
    binding: Callable[[], ProviderBinding],
    environment: Callable[[], ActiveModelEnvironment],
    tool_catalog: ToolCatalog,
    permission_policy: PermissionPolicy,
    permission_prompter: PermissionPrompter,
    workspace: Workspace,
    max_steps: int | None = None,
    prompt_registry: PromptRegistry | None = None,
    allow_permission_updates: bool = True,
    attachment_sources: tuple[DerivedAttachmentSource, ...] = (),
) -> AgentRunComponents:
    tool_executor = ToolExecutor(
        tools=tool_catalog.snapshot(),
        policy=permission_policy,
        prompter=permission_prompter,
        workspace=workspace,
        update_applier=(
            PermissionUpdateApplier(
                permission_policy, SettingsStore(settings.paths)
            ).apply
            if allow_permission_updates
            else lambda _: None
        ),
    )
    planner = ContextPlanner(
        window=ContextWindow(settings.context_chars),
        prompt=prompt_registry or build_system_prompt_registry(settings.cwd),
        max_output_tokens=settings.max_output_tokens,
        user_context_resolver=AgentsUserContextResolver(settings.cwd),
        attachment_resolver=DerivedAttachmentResolver(
            (TodoReminderAttachmentSource(), *attachment_sources)
        ),
        binding_resolver=binding,
        model_environment=environment,
    )
    context = ContextEngine(planner, ContextCompactor(model_call))
    tool_round = ToolRoundExecutor(
        tool_executor,
        max_parallel_calls=settings.max_parallel_tool_calls,
    )
    return AgentRunComponents(
        agent=AgentEngine(
            model_call=model_call,
            tool_round=tool_round,
            context=context,
            tool_catalog=tool_catalog,
            max_steps=settings.max_steps if max_steps is None else max_steps,
        ),
        context=context,
        tool_executor=tool_executor,
    )


def _assemble_agent(
    settings: AgentSettings,
    session_id: str | None = None,
    *,
    permission_prompter: PermissionPrompter | None = None,
    mcp_transport_factory: McpTransportFactory | None = None,
) -> ApplicationAssembly:
    actual_session_id = session_id or str(uuid4())
    descriptor = settings.model_descriptor or resolve_without_network(
        settings.protocol,
        settings.base_url,
        settings.model,
        settings.model_limits,
    )
    model_environment = resolve_environment(
        descriptor,
        requested_output_tokens=settings.max_output_tokens,
        configured_trigger_tokens=settings.compact.trigger_input_tokens,
        discovered_at=settings.model_discovered_at,
        discovery_error=settings.model_discovery_error,
    )
    session = Session(
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
            model_limits=descriptor.limits,
            model_limit_source=descriptor.source.value,
            compact_trigger_tokens=model_environment.compact_trigger_tokens,
        ),
    )
    tool_catalog = ToolCatalog()
    tool_catalog.register_source(ToolSourceId("builtin", "core"), builtin_tools())
    tool_catalog.register_source(ToolSourceId("feature", "todos"), (TodoWriteTool(),))
    mcp = McpRuntime(
        enabled=settings.mcp_enabled,
        servers=tuple(
            McpServerSpec(
                name=server.name,
                command=server.command,
                args=server.args,
                env_from=server.env_from,
                cwd=settings.cwd,
                scope=McpServerScope(server.scope.value),
                enabled=server.enabled,
                start_allowed=server.scope is not SettingsScope.PROJECT,
                startup_timeout_seconds=server.startup_timeout_seconds,
                call_timeout_seconds=server.call_timeout_seconds,
            )
            for server in settings.mcp_servers
        ),
        catalog=tool_catalog,
        transport_factory=(
            mcp_transport_factory
            if mcp_transport_factory is not None
            else StdioMcpTransportFactory(os.environ)
        ),
        deferred_tool_threshold=settings.mcp_deferred_tool_threshold,
    )
    skill_roots = [
        SkillSearchRoot(
            SkillSourceId(200, SkillSourceKind.USER, "config"),
            settings.paths.config_home / "skills",
        ),
        SkillSearchRoot(
            SkillSourceId(100, SkillSourceKind.BUILTIN, "package"),
            Path(__file__).resolve().parent / "builtin_skills",
        ),
    ]
    if not settings.paths.project_config_collides_with_user_storage:
        skill_roots.insert(
            0,
            SkillSearchRoot(
                SkillSourceId(300, SkillSourceKind.PROJECT, "workspace"),
                settings.paths.project_config_dir / "skills",
            ),
        )
    skills = SkillRuntime(
        enabled=settings.skills_enabled,
        roots=tuple(skill_roots),
        tool_catalog=tool_catalog,
    )
    prompter = permission_prompter or (
        TerminalPrompter() if settings.interactive else HeadlessPrompter()
    )
    permission_policy = PermissionPolicy(
        mode=settings.permission_mode,
        rules=settings.permission_rules,
    )
    restore_skill_permissions(permission_policy, session.conversation)
    workspace = Workspace(settings.cwd)
    connection = ProviderConnection(
        id=settings.provider_id,
        protocol=settings.protocol,
        model=settings.model,
        base_url=settings.base_url,
        api_key=settings.api_key,
        credential_source=settings.credential_source,
        reasoning=settings.reasoning,
        limits=settings.model_limits,
        compact=settings.compact,
    )
    provider = ProviderRouter(connection)
    provider_leases = ProviderLeaseRegistry(connection)
    provider_runtime = ProviderRuntime(
        provider,
        provider_leases,
        model_environment,
    )
    background_notifications: BackgroundTaskNotificationSource | None = None

    def extra_attachment_sources() -> tuple[DerivedAttachmentSource, ...]:
        if background_notifications is None:
            return ()
        return (background_notifications,)

    def build_run(
        spec: AgentRunSpec,
        lease: ProviderClientLease,
        environment: ActiveModelEnvironment,
    ) -> AgentRunComponents:
        model_call: ModelClient = lease
        if spec.max_tokens is not None:
            model_call = TokenBudgetModelClient(lease, spec.max_tokens)
        return _build_agent_components(
            settings,
            model_call=model_call,
            binding=lambda: lease.binding,
            environment=lambda: environment,
            tool_catalog=spec.tool_catalog or tool_catalog,
            permission_policy=spec.permission_policy or permission_policy,
            permission_prompter=prompter,
            workspace=workspace,
            max_steps=spec.max_steps,
            prompt_registry=spec.prompt_registry,
            allow_permission_updates=spec.allow_permission_updates,
            attachment_sources=(
                SkillListingAttachmentSource(skills.catalog),
                *extra_attachment_sources(),
            ),
        )

    run_factory = AgentRunFactory(
        provider_leases,
        provider_runtime.environment,
        build_run,
    )
    tasks = TaskSupervisor()
    if settings.subagents_enabled:
        subagents = SubagentController(
            runs=run_factory,
            tasks=tasks,
            project_state_dir=settings.paths.project_state_dir,
            definitions=build_subagent_definitions(settings.cwd),
            limits=SubagentLimits(
                max_depth=settings.subagent_max_depth,
                max_active_children=settings.subagent_max_active_children,
                max_steps=settings.subagent_max_steps,
                max_tokens=settings.subagent_max_tokens,
                timeout_seconds=settings.subagent_timeout_seconds,
            ),
            background_enabled=settings.background_tasks_enabled,
        )
        if settings.background_tasks_enabled:
            background_notifications = BackgroundTaskNotificationSource(subagents)
        parent = SubagentParentContext(actual_session_id)
        feature_tools: list[Tool] = [
            SubagentTool(
                subagents,
                parent=parent,
                policy=permission_policy,
            )
        ]
        if settings.background_tasks_enabled:
            feature_tools.extend(
                (
                    TaskListTool(subagents, parent=parent),
                    TaskOutputTool(subagents, parent=parent),
                    TaskCancelTool(subagents, parent=parent),
                )
            )
        tool_catalog.register_source(
            ToolSourceId("feature", "subagents"),
            feature_tools,
        )
    initial_tools = tool_catalog.snapshot()
    components = _build_agent_components(
        settings,
        model_call=provider,
        binding=lambda: provider.binding,
        environment=provider_runtime.environment,
        tool_catalog=tool_catalog,
        permission_policy=permission_policy,
        permission_prompter=prompter,
        workspace=workspace,
        attachment_sources=(
            SkillListingAttachmentSource(skills.catalog),
            *extra_attachment_sources(),
        ),
    )
    return ApplicationAssembly(
        agent=components.agent,
        context=components.context,
        provider=provider,
        tool_catalog=tool_catalog,
        initial_tools=initial_tools,
        permissions=permission_policy,
        tool_context=components.tool_executor.context,
        tool_executor=components.tool_executor,
        provider_runtime=provider_runtime,
        run_factory=run_factory,
        tasks=tasks,
        mcp=mcp,
        skills=skills,
        session=session,
    )


def bootstrap_chat(
    settings: AgentSettings,
    session_id: str | None = None,
) -> ChatService:
    """Assemble the concrete Chat service used by every host."""

    prompter = DeferredPermissionPrompter()
    assembled = _assemble_agent(settings, session_id, permission_prompter=prompter)
    return ChatService(
        agent=assembled.agent,
        context=assembled.context,
        tool_executor=assembled.tool_executor,
        settings=settings,
        permission_prompter=prompter,
        provider_manager=ProviderManager(settings.paths),
        state=AppState(
            workspace=WorkspaceState(assembled.tool_executor.workspace),
            session=assembled.session,
            permissions=PermissionState(assembled.permissions),
            provider=assembled.provider_runtime,
            tools=ToolState(assembled.tool_catalog),
            tasks=assembled.tasks,
            runs=assembled.run_factory,
            mcp=assembled.mcp,
            skills=assembled.skills,
        ),
        attachment_loader=AttachmentLoader(
            WorkspaceAttachmentReader(settings.cwd, assembled.permissions)
        ),
        path_suggester=WorkspacePathSuggester(settings.cwd),
    )


async def _submit(options: CliOptions, resolver: SettingsResolver) -> int:
    settings = resolver.resolve(options.settings_overrides, interactive=False)
    if settings.paths.providers_path.exists():
        settings = await discover_active_model(settings)
    runtime = bootstrap_chat(settings, options.session_id)
    try:
        result = await runtime.submit(options.prompt or "")
    finally:
        await runtime.close()
    if isinstance(result, TurnSucceeded):
        print(result.text or "<no text response>")
        return 0
    assert isinstance(result, MaxStepsReached)
    print(f"Error: Reached max steps ({result.max_steps})", file=sys.stderr)
    return 1


async def run(options: CliOptions, resolver: SettingsResolver) -> int:
    if options.prompt is not None:
        return await _submit(options, resolver)
    settings = resolver.resolve(options.settings_overrides, interactive=True)
    if settings.paths.providers_path.exists():
        settings = await discover_active_model(settings)
    runtime = bootstrap_chat(settings, options.session_id)
    try:
        await MyCodeTui(runtime).run()
    finally:
        await runtime.close()
    return 0


def _run_async(task: Coroutine[Any, Any, int]) -> int:
    try:
        return asyncio.run(task)
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> None:
    try:
        options = parse_cli(argv)
        resolver = SettingsResolver.for_workspace(options.cwd)
        initialize_user_storage(resolver.paths)
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
    if isinstance(options, AuthOptions):
        provider_id = resolver.active_provider_id(options.provider_override)
        protocol = (
            ProviderProfileStore(resolver.paths.providers_path)
            .load()[provider_id]
            .protocol.value
        )
        try:
            raise SystemExit(
                run_auth_command(
                    options, resolver.paths, provider_id, protocol=protocol
                )
            )
        except (EOFError, KeyboardInterrupt):
            print("Cancelled.", file=sys.stderr)
            raise SystemExit(130) from None
        except ValueError as error:
            print(f"Error: {error}", file=sys.stderr)
            raise SystemExit(2) from error
    raise SystemExit(_run_async(run(options, resolver)))
