"""Pure text and table projections used by the terminal frontend."""

from datetime import UTC, datetime
from io import StringIO

from rich.console import Console, RenderableType

from my_code.chat.status import ContextStatus
from my_code.chat.views import (
    BackgroundTaskView,
    CapabilitiesView,
    SessionUsageView,
    SubagentTaskView,
)
from my_code.tui.theme import TuiTheme
from my_code.tui.widgets import capability_table, history_message


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
    "format_context_usage",
    "render_context_status",
    "render_mcp",
    "render_skills",
    "render_tasks",
    "render_agent_view",
    "render_tools",
    "render_usage",
]
