"""Application composition root and process entrypoint."""

import asyncio
import sys
from collections.abc import Coroutine
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from nano_code.agent.engine import AgentEngine
from nano_code.auth.credentials import CredentialStore
from nano_code.chat.events import MaxStepsReached, TurnSucceeded
from nano_code.chat.permissions import DeferredPermissionPrompter
from nano_code.chat.service import ChatService
from nano_code.cli.arguments import AuthOptions, CliOptions, parse_cli
from nano_code.cli.auth import run_auth_command
from nano_code.config.paths import NanoCodePaths, SettingsScope
from nano_code.config.permission_updates import PermissionUpdateApplier
from nano_code.config.providers import (
    DEFAULT_MODEL,
    DEFAULT_PROVIDER_ID,
    ProviderProfile,
    ProviderProfileStore,
)
from nano_code.config.settings import AgentSettings, SettingsResolver
from nano_code.config.store import SettingsLayer, SettingsStore
from nano_code.context.attachments.sources import DerivedAttachmentResolver
from nano_code.context.compaction import CompactionCoordinator, CompactionService
from nano_code.context.planner import ContextBuilder
from nano_code.context.user_context import AgentsUserContextResolver
from nano_code.context.window import ContextWindow
from nano_code.features.file_mentions.loader import AttachmentLoader
from nano_code.features.file_mentions.reader import WorkspaceAttachmentReader
from nano_code.features.file_mentions.suggestions import WorkspacePathSuggester
from nano_code.features.todos.reminder import TodoReminderAttachmentSource
from nano_code.features.todos.tool import TodoWriteTool
from nano_code.model.capabilities import ActiveModelState, resolve_environment
from nano_code.permissions.models import PermissionPrompter
from nano_code.permissions.policy import PermissionPolicy
from nano_code.permissions.prompt import HeadlessPrompter, TerminalPrompter
from nano_code.prompts.system import build_system_prompt_registry
from nano_code.providers.discovery import ModelDiscoveryService, resolve_without_network
from nano_code.providers.manager import ProviderManager
from nano_code.providers.model_cache import ModelCatalogCache
from nano_code.providers.router import ProviderConnection, ProviderRouter
from nano_code.sessions.models import SessionStart
from nano_code.sessions.session import Session
from nano_code.sessions.store import SessionStore
from nano_code.tools.base import ToolContext
from nano_code.tools.builtin import builtin_tools
from nano_code.tools.executor import ToolExecutor
from nano_code.tools.registry import ToolRegistry
from nano_code.tools.result_store import ToolResultStore
from nano_code.tools.round_executor import ToolRoundExecutor
from nano_code.tui.app import NanoCodeTui
from nano_code.workspace.local import Workspace


@dataclass(frozen=True, slots=True)
class StorageInitialization:
    created_settings: bool
    created_providers: bool
    created_credentials: bool


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
    ActiveModelState,
    Session,
]:
    actual_session_id = session_id or str(uuid4())
    descriptor = settings.model_descriptor or resolve_without_network(
        settings.protocol,
        settings.base_url,
        settings.model,
        settings.model_limits,
    )
    active_model_state = ActiveModelState(
        resolve_environment(
            descriptor,
            requested_output_tokens=settings.max_output_tokens,
            configured_trigger_tokens=settings.compact.trigger_input_tokens,
            discovered_at=settings.model_discovered_at,
            discovery_error=settings.model_discovery_error,
        )
    )
    model_environment = active_model_state.get()
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
            model_limits=descriptor.limits,
            model_limit_source=descriptor.source.value,
            compact_trigger_tokens=model_environment.compact_trigger_tokens,
        ),
    )
    session = Session(repository)
    registry = ToolRegistry((*builtin_tools(), TodoWriteTool()))
    prompter = permission_prompter or (
        TerminalPrompter() if settings.interactive else HeadlessPrompter()
    )
    permission_policy = PermissionPolicy(
        mode=settings.permission_mode,
        rules=settings.permission_rules,
    )
    workspace = Workspace(settings.cwd)
    tool_context = ToolContext(workspace)
    tool_executor = ToolExecutor(
        registry=registry,
        policy=permission_policy,
        prompter=prompter,
        workspace=workspace,
        result_store=ToolResultStore(
            settings.paths.tool_results_dir(actual_session_id)
        ),
        update_applier=PermissionUpdateApplier(
            permission_policy, SettingsStore(settings.paths)
        ).apply,
    )
    provider = ProviderRouter(
        ProviderConnection(
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
    )
    context = ContextBuilder(
        window=ContextWindow(settings.context_chars),
        prompt=build_system_prompt_registry(settings.cwd),
        tools=registry.definitions,
        max_output_tokens=settings.max_output_tokens,
        user_context_resolver=AgentsUserContextResolver(settings.cwd),
        attachment_resolver=DerivedAttachmentResolver(
            (TodoReminderAttachmentSource(),)
        ),
        binding_resolver=lambda: provider.binding,
        active_model_state=active_model_state,
    )
    tool_round = ToolRoundExecutor(tool_executor)
    engine = AgentEngine(
        model_call=provider,
        tool_round=tool_round,
        context=context,
        compactor=CompactionCoordinator(context, CompactionService(provider)),
        max_steps=settings.max_steps,
    )
    return (
        engine,
        provider,
        registry,
        permission_policy,
        tool_context,
        tool_executor,
        active_model_state,
        session,
    )


def bootstrap_chat(
    settings: AgentSettings,
    session_id: str | None = None,
) -> ChatService:
    """Assemble the concrete Chat service used by every host."""

    prompter = DeferredPermissionPrompter()
    assembled = _assemble_agent(settings, session_id, permission_prompter=prompter)
    engine, provider, _, _, _, tool_executor, active_model_state, session = assembled
    return ChatService(
        agent=engine,
        settings=settings,
        permission_prompter=prompter,
        provider_manager=ProviderManager(settings.paths),
        provider_router=provider,
        active_model_state=active_model_state,
        session=session,
        tool_result_store=lambda active_id: ToolResultStore(
            settings.paths.tool_results_dir(active_id)
        ),
        attachment_loader=AttachmentLoader(
            WorkspaceAttachmentReader(settings.cwd, tool_executor.policy)
        ),
        path_suggester=WorkspacePathSuggester(settings.cwd),
    )


async def _submit(options: CliOptions, resolver: SettingsResolver) -> int:
    settings = resolver.resolve(options.settings_overrides, interactive=False)
    if settings.paths.providers_path.exists():
        settings = await discover_active_model(settings)
    result = await bootstrap_chat(settings, options.session_id).submit(
        options.prompt or ""
    )
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
    await NanoCodeTui(bootstrap_chat(settings, options.session_id)).run()
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
