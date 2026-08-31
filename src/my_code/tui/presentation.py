"""Pure text and table projections used by the terminal frontend."""

from datetime import UTC, datetime
from io import StringIO

from rich.console import Console, RenderableType
from rich.text import Text

from my_code.application.contracts.events import CompactionTrigger
from my_code.application.contracts.status import ApplicationStatus, ContextUsageView
from my_code.application.contracts.views import (
    BackgroundTaskView,
    CapabilitiesView,
    SessionUsageView,
    SubagentTaskView,
)
from my_code.tui.theme import TuiTheme
from my_code.tui.widgets import (
    capability_table,
    field_table,
    history_message,
    information_card,
)
from my_code.version import __version__


def render_status_card(
    status: ApplicationStatus, context: ContextUsageView
) -> RenderableType:
    used = context.projected_tokens
    remaining = max(0, context.input_limit_tokens - used)
    percent = (
        round(remaining * 100 / context.input_limit_tokens)
        if context.input_limit_tokens
        else 0
    )
    provider = status.provider_id
    if status.base_url:
        provider = f"{provider} · {status.base_url}"
    capabilities = (
        f"{status.tool_count} tools · {status.skill_count} skills · "
        f"{status.mcp_connected_count}/{status.mcp_server_count} MCP"
    )
    rows: tuple[tuple[str, RenderableType | str], ...] = (
        ("Model", status.model),
        ("Provider", provider),
        ("Directory", status.cwd),
        ("Permissions", status.permission_mode),
        ("Command environment", status.execution_environment),
        ("Authentication", status.credential_source),
        ("Session", status.session_id),
        ("Capabilities", capabilities),
        (
            "Context window",
            Text.assemble(
                (f"{percent}% left"),
                (
                    f" ({format_token_k(used)} used / "
                    f"{format_token_k(context.input_limit_tokens)})",
                    "dim",
                ),
            ),
        ),
        (
            "Entries",
            f"{status.context_entry_count} context · "
            f"{status.conversation_entry_count} conversation",
        ),
    )
    return information_card(f"my-code v{__version__} · Status", field_table(rows))


def render_context_card(status: ContextUsageView) -> RenderableType:
    measured = (
        "reported + estimated delta"
        if status.measurement == "reported"
        else "estimated"
    )
    trigger_source = (
        "auto" if status.configured_compact_trigger_tokens is None else "configured"
    )
    rows: list[tuple[str, RenderableType | str]] = [
        ("Context", format_context_usage(status)),
        ("Measured by", measured),
        (
            "Compact at",
            f"{format_token_k(status.compact_trigger_tokens)} ({trigger_source})",
        ),
        (
            "Compactions",
            f"{status.replacement_count} micro · {status.compact_count} full",
        ),
    ]
    if status.warning:
        rows.append(("Warning", Text(status.warning, style="yellow")))
    return information_card("Context", field_table(tuple(rows)))


def render_context_status(status: ContextUsageView) -> str:
    measured = (
        "reported + estimated delta"
        if status.measurement == "reported"
        else "estimated"
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


def format_context_usage(status: ContextUsageView) -> str:
    used = status.projected_tokens
    return f"{format_token_k(used)} / {format_token_k(status.input_limit_tokens)}"


def compaction_activity_label(trigger: CompactionTrigger) -> str:
    return {
        "manual": "Compacting context…",
        "auto": "Compacting context automatically…",
        "reactive": "Compacting context after context overflow…",
    }[trigger]


def compaction_completed_message(
    trigger: CompactionTrigger, status: ContextUsageView
) -> str:
    source = {
        "manual": "manual",
        "auto": "automatic",
        "reactive": "context overflow recovery",
    }[trigger]
    return f"Context compacted · {source} · {format_context_usage(status)}"


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


def render_usage_card(usage: SessionUsageView) -> RenderableType:
    rows: tuple[tuple[str, str], ...] = (
        ("Requests", str(usage.request_count)),
        ("Input tokens", str(usage.input_tokens)),
        ("Cache creation input", str(usage.cache_creation_input_tokens)),
        ("Cache read input", str(usage.cache_read_input_tokens)),
        ("Output tokens", str(usage.output_tokens)),
        ("Current context", format_context_usage(usage.context)),
    )
    return information_card("Usage", field_table(rows))


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
    return information_card(
        f"Tools ({len(rows)}) · search={capabilities.tool_search_mode}",
        capability_table("", rows or (("No active tools",),)),
    )


def render_skills(capabilities: CapabilitiesView) -> RenderableType:
    rows = [
        (skill.name, skill.source, skill.description) for skill in capabilities.skills
    ]
    rows.extend(
        (f"! {item.code}", item.source, item.message)
        for item in capabilities.skill_diagnostics
    )
    return information_card(
        f"Skills ({len(capabilities.skills)})",
        capability_table("", tuple(rows) or (("No skills",),)),
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
    return information_card(
        "MCP servers",
        capability_table("", rows or (("No MCP servers configured",),)),
    )


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
    return information_card(
        title,
        capability_table("", rows or (("No background tasks for this session",),)),
    )


def render_agent_view(
    task: SubagentTaskView,
    *,
    scroll: int = 0,
    width: int = 100,
    theme: TuiTheme | None = None,
) -> str:
    mode = "background" if task.background else "foreground"
    lines = [
        f"{task.description} · {task.agent_type} · {mode}",
        f"Status: {task.status} · Elapsed: {_elapsed(task)} · Run: {task.run_id}",
        f"Usage: {task.input_tokens} input / {task.output_tokens} output tokens",
    ]
    if task.error:
        lines.append(f"Error: {task.error}")
    entries = list(task.transcript)
    end = max(0, len(entries) - scroll)
    start = max(0, end - 30)
    stream = StringIO()
    console = Console(file=stream, width=max(width, 40), color_system=None)
    for entry in entries[start:end]:
        console.print(history_message(entry, theme))
    rendered = stream.getvalue().rstrip()
    if rendered:
        lines.append(rendered)
    lines.append("PageUp/PageDown scroll · End live tail · Esc main")
    return "\n".join(lines)


def _elapsed(task: SubagentTaskView) -> str:
    start = task.started_at or task.created_at
    end = task.finished_at
    try:
        started = datetime.fromisoformat(start)
        finished = datetime.fromisoformat(end) if end is not None else datetime.now(UTC)
        seconds = max(0, int((finished - started).total_seconds()))
    except (TypeError, ValueError):
        return "unknown"
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return (
        f"{hours}h {minutes:02d}m {seconds:02d}s"
        if hours
        else f"{minutes}m {seconds:02d}s"
    )


__all__ = [
    "compaction_activity_label",
    "compaction_completed_message",
    "format_context_usage",
    "render_context_status",
    "render_context_card",
    "render_mcp",
    "render_skills",
    "render_tasks",
    "render_status_card",
    "render_agent_view",
    "render_tools",
    "render_usage",
    "render_usage_card",
]
