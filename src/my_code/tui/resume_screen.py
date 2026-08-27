"""Formatting helpers for the bottom session picker."""

from datetime import UTC, datetime

from my_code.sessions.catalog import SessionSummary


def render_session(summary: SessionSummary, now: datetime | None = None) -> str:
    current = now or datetime.now(UTC)
    return f"{summary.title}  ·  {_relative_time(summary.updated_at, current)}"


def _relative_time(value: datetime, now: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    seconds = max(0, int((now.astimezone(UTC) - value.astimezone(UTC)).total_seconds()))
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 30:
        return f"{days}d ago"
    months = days // 30
    return f"{months}mo ago" if months < 12 else f"{days // 365}y ago"


__all__ = ["render_session"]
