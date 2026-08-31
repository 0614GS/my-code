from prompt_toolkit.formatted_text import fragment_list_to_text

from my_code.tui.activity_indicator import ActivityIndicator, format_elapsed


def test_elapsed_format_uses_compact_seconds_minutes_and_hours() -> None:
    assert format_elapsed(0) == "0s"
    assert format_elapsed(59) == "59s"
    assert format_elapsed(60) == "1m 00s"
    assert format_elapsed(185) == "3m 05s"
    assert format_elapsed(3723) == "1h 02m 03s"


def test_activity_phase_update_preserves_invocation_clock_and_animates() -> None:
    now = 10.0
    indicator = ActivityIndicator(clock=lambda: now)
    owner = indicator.begin("my-code is working…", interruptible=True)

    first = fragment_list_to_text(indicator.text(now=10.0))
    second = fragment_list_to_text(indicator.text(now=10.11))
    assert first.startswith("⠋ my-code is working…")
    assert second.startswith("⠙ my-code is working…")
    assert "0s · Esc to interrupt" in first

    assert indicator.update(owner, "Compacting context automatically…") is True
    updated = fragment_list_to_text(indicator.text(now=75.0))
    assert "Compacting context automatically…" in updated
    assert "1m 05s · Esc to interrupt" in updated


def test_stale_activity_owner_cannot_update_or_clear_newer_operation() -> None:
    indicator = ActivityIndicator(clock=lambda: 0.0)
    stale = indicator.begin("first")
    current = indicator.begin("second")

    assert indicator.update(stale, "stale update") is False
    assert indicator.end(stale) is False
    assert "second" in fragment_list_to_text(indicator.text(now=1.0))
    assert indicator.end(current) is True
    assert fragment_list_to_text(indicator.text()) == ""
