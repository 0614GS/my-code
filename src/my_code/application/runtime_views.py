"""Pure projections from explicit runtime snapshots to host-safe contracts."""

from my_code.application.contracts.status import ApplicationStatus, ContextUsageView
from my_code.application.contracts.views import (
    CapabilitiesView,
    CapabilityDiagnosticView,
    McpServerView,
    SessionUsageView,
    SkillCapabilityView,
    ToolCapabilityView,
)
from my_code.config.settings import AgentSettings
from my_code.context.engine import ContextEngine
from my_code.context.session_cache import SessionContextCache
from my_code.conversation.models import AssistantMessage
from my_code.features.todos.projection import project_todos
from my_code.mcp.models import McpServerSnapshot
from my_code.model.tool_search import ToolSearchMode
from my_code.providers.router import ProviderConnection
from my_code.sessions.session import Session
from my_code.skills.catalog import SkillCatalogSnapshot
from my_code.tools.catalog import ToolCatalogSnapshot
from my_code.tools.discovery import ToolExposureSnapshot, restored_discoveries


def project_context_status(
    context: ContextEngine,
    session: Session,
    runtime: SessionContextCache,
    tools: ToolCatalogSnapshot,
) -> ContextUsageView:
    budget = context.inspect(
        session.context_planning_state(), runtime, tools=tools.definitions
    )
    return ContextUsageView(
        reported_base_tokens=budget.reported_base_tokens,
        estimated_delta_tokens=budget.estimated_delta_tokens,
        projected_tokens=budget.projected_tokens,
        reserved_output_tokens=budget.reserved_output_tokens,
        context_entry_count=session.context_entry_count,
        conversation_entry_count=session.conversation_entry_count,
        replacement_count=session.content_replacement_count,
        compact_count=session.compact_count,
        input_limit_tokens=budget.input_limit_tokens,
        compact_trigger_tokens=budget.compact_trigger_tokens,
        remaining_input_tokens=budget.remaining_input_tokens,
        measurement=budget.measurement,
        model_limit_source=budget.model_limit_source.value,
        configured_compact_trigger_tokens=budget.configured_compact_trigger_tokens,
        warning=budget.warning,
    )


def project_capabilities(
    *,
    tools: ToolCatalogSnapshot,
    skills: SkillCatalogSnapshot,
    mcp_servers: tuple[McpServerSnapshot, ...],
    tool_search_mode: ToolSearchMode,
    session: Session,
) -> CapabilitiesView:
    exposure = ToolExposureSnapshot.build(
        tools, tool_search_mode, restored_discoveries(session.conversation)
    )
    return CapabilitiesView(
        tools=tuple(
            ToolCapabilityView(
                registration.tool.definition.name,
                registration.tool.definition.description,
                str(registration.source),
                (
                    "searched"
                    if registration.tool.definition.name in exposure.searched
                    else registration.tool.exposure.value
                ),
                (
                    "direct"
                    if registration.tool.definition.name in exposure.direct_tools
                    else "via InvokeSearchedTool"
                    if registration.tool.definition.name in exposure.searched
                    else "via ToolSearch"
                ),
            )
            for registration in tools.registrations
        ),
        skills=tuple(
            SkillCapabilityView(
                entry.name,
                entry.description,
                str(entry.source),
                entry.compatibility,
            )
            for entry in skills.entries
        ),
        skill_diagnostics=tuple(
            CapabilityDiagnosticView(
                str(diagnostic.source),
                diagnostic.code.value,
                diagnostic.message,
            )
            for diagnostic in skills.diagnostics
        ),
        mcp_servers=tuple(
            McpServerView(
                server.name,
                server.state.value,
                server.tool_names,
                (
                    CapabilityDiagnosticView(
                        server.name,
                        server.diagnostic.code.value,
                        server.diagnostic.message,
                    )
                    if server.diagnostic is not None
                    else None
                ),
            )
            for server in mcp_servers
        ),
        tool_search_mode=tool_search_mode.value,
    )


def project_runtime_status(
    *,
    settings: AgentSettings,
    session: Session,
    connection: ProviderConnection,
    permission_mode: str,
    execution_environment: str,
    capabilities: CapabilitiesView,
) -> ApplicationStatus:
    return ApplicationStatus(
        session_id=session.session_id,
        cwd=str(settings.cwd),
        provider_id=connection.id,
        base_url=connection.base_url,
        model=connection.model,
        permission_mode=permission_mode,
        credential_source=connection.credential_source.value,
        context_entry_count=session.context_entry_count,
        conversation_entry_count=session.conversation_entry_count,
        todos=project_todos(session.conversation).todos,
        tool_count=len(capabilities.tools),
        skill_count=len(capabilities.skills),
        mcp_connected_count=sum(
            server.state == "connected" for server in capabilities.mcp_servers
        ),
        mcp_server_count=len(capabilities.mcp_servers),
        execution_environment=execution_environment,
        collaboration_mode=session.collaboration_mode,
    )


def project_session_usage(
    session: Session, context_status: ContextUsageView
) -> SessionUsageView:
    usages = (
        message.usage
        for message in session.conversation
        if isinstance(message, AssistantMessage)
    )
    request_count = input_tokens = output_tokens = cache_creation = cache_read = 0
    for usage in usages:
        request_count += 1
        input_tokens += usage.input_tokens
        output_tokens += usage.output_tokens
        cache_creation += usage.cache_creation_input_tokens
        cache_read += usage.cache_read_input_tokens
    return SessionUsageView(
        request_count,
        input_tokens,
        cache_creation,
        cache_read,
        output_tokens,
        context_status,
    )


__all__ = [
    "project_capabilities",
    "project_context_status",
    "project_runtime_status",
    "project_session_usage",
]
