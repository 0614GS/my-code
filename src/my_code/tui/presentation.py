"""Pure text and table projections used by the terminal frontend."""

from rich.console import RenderableType

from my_code.chat.status import ContextStatus
from my_code.chat.views import BackgroundTaskView, CapabilitiesView, SessionUsageView
from my_code.tui.widgets import capability_table


def render_context_status(status: ContextStatus) -> str:
    measured = (
        "provider calibrated"
        if status.measurement == "reported_calibrated"
        else "local estimate"
    )
    trigger_source = (
        "auto" if status.configured_compact_trigger_tokens is None else "configured"
    )
    lines = [
        f"Context: {format_context_usage(status)}",
        f"Measured by: {measured}",
        f"Compact at: {format_token_k(status.compact_trigger_tokens)} "
        f"({trigger_source})",
        f"Compactions: {status.replacement_count} micro · {status.compact_count} full",
    ]
    if status.warning:
        lines.append(f"Warning: {status.warning}")
    return "\n".join(lines)


def format_context_usage(status: ContextStatus) -> str:
    used = status.input_tokens or status.estimated_input_tokens
    return f"{format_token_k(used)} / {format_token_k(status.input_limit_tokens)}"


def format_token_k(tokens: int) -> str:
    value = f"{tokens / 1000:.1f}".removesuffix(".0")
    return f"{value}k"


def render_usage(usage: SessionUsageView) -> str:
    return "\n".join(
        (
            f"Requests: {usage.request_count}",
            f"Input tokens: {usage.input_tokens}",
            f"Cache creation input: {usage.cache_creation_input_tokens}",
            f"Cache read input: {usage.cache_read_input_tokens}",
            f"Output tokens: {usage.output_tokens}",
            f"Current context: {format_context_usage(usage.context)}",
        )
    )


def render_tools(capabilities: CapabilitiesView) -> RenderableType:
    rows = tuple(
        (
            tool.name,
            f"{tool.exposure}/{tool.access}",
            tool.source,
            tool.description,
        )
        for tool in capabilities.tools
    )
    return capability_table(
        f"Tools ({len(rows)}) · search={capabilities.tool_search_mode}",
        rows or (("No active tools",),),
    )


def render_skills(capabilities: CapabilitiesView) -> RenderableType:
    rows = [
        (skill.name, skill.source, skill.description) for skill in capabilities.skills
    ]
    rows.extend(
        (f"! {item.code}", item.source, item.message)
        for item in capabilities.skill_diagnostics
    )
    return capability_table(
        f"Skills ({len(capabilities.skills)})", tuple(rows) or (("No skills",),)
    )


def render_mcp(capabilities: CapabilitiesView) -> RenderableType:
    rows = tuple(
        (
            server.name,
            server.state,
            f"{len(server.tool_names)} tools"
            + (f" · {server.diagnostic.message}" if server.diagnostic else ""),
        )
        for server in capabilities.mcp_servers
    )
    return capability_table("MCP servers", rows or (("No MCP servers configured",),))


def render_tasks(tasks: tuple[BackgroundTaskView, ...]) -> RenderableType:
    rows = tuple(
        (
            task.task_type,
            task.status,
            task.summary,
            task.output_path or "",
            task.error or "",
        )
        for task in tasks
    )
    title = "Background tasks · cancellation is performed by Agent TaskCancel"
    return capability_table(title, rows or (("No background tasks for this session",),))


__all__ = [
    "format_context_usage",
    "render_context_status",
    "render_mcp",
    "render_skills",
    "render_tasks",
    "render_tools",
    "render_usage",
]
