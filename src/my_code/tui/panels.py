"""Rendering for temporary bottom panels.

Panel navigation and mutations remain in the application controller; this module
only turns safe frontend state into compact terminal text.
"""

from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.utils import get_cwidth

from my_code.chat.permissions import PermissionRequest
from my_code.chat.views import SubagentTaskView
from my_code.providers.manager import ProviderView
from my_code.sessions.catalog import SessionSummary
from my_code.tui.picker import PickerRow, PickerState, PickerView
from my_code.tui.provider_screen import ProviderForm
from my_code.tui.resume_screen import render_session_parts


def permission_panel(
    request: PermissionRequest, mode: str
) -> PickerView | FormattedText:
    if mode == "feedback":
        return _message("Permission denied with feedback", "Enter to send · Esc deny")
    if request.tool_name == "Bash":
        choices = [
            PickerRow("allow", "Yes"),
            PickerRow(
                "second",
                f'Yes, and don\'t ask again for "{_suggestion_scope(request)}"',
            ),
            PickerRow("third", "No"),
        ]
        shortcut = "↑↓ select · Enter confirm · 1–3 shortcut · Esc deny"
    else:
        choices = [
            PickerRow("allow", "Yes"),
            PickerRow("second", "No"),
            PickerRow("third", "No, with feedback"),
        ]
        shortcut = "↑↓ select · Enter confirm · 1–4 shortcut · Esc deny"
    if request.tool_name != "Bash" and request.suggestions:
        choices.append(PickerRow("remember", "Yes, and remember"))
    return PickerView(
        f"Tool use · {request.presentation.display_name}"
        f" ({request.presentation.summary})\n{request.message}",
        tuple(choices),
        shortcut,
    )


def full_access_panel() -> PickerView:
    return PickerView(
        "Full access can modify files and run commands without asking.\n"
        "No OS sandbox is active for this process.",
        (
            PickerRow("deny", "No, keep current mode"),
            PickerRow("allow", "Yes, enable Full access"),
        ),
        "↑↓ select · Enter confirm · Esc keep current mode",
    )


def agent_select_panel(agents: tuple[SubagentTaskView, ...]) -> PickerView:
    return PickerView(
        "Main session · F6 cycles through agent views",
        (
            PickerRow("main", "Main session"),
            *(
                PickerRow(
                    item.task_id,
                    (
                        "○ "
                        if item.status in {"succeeded", "failed", "cancelled"}
                        else "● "
                    )
                    + f"{item.description} · {item.status} · "
                    + ("background" if item.background else "foreground"),
                )
                for item in agents
            ),
        ),
        "↑↓ select · Enter view · Esc close",
    )


def _suggestion_scope(request: PermissionRequest) -> str:
    if not request.suggestions or not request.suggestions[0].rules:
        return "this exact command"
    return request.suggestions[0].rules[0].rule_content or request.tool_name


def resume_panel(sessions: tuple[SessionSummary, ...]) -> PickerView | FormattedText:
    if not sessions:
        return _message("No conversations found to resume", "Esc close")
    return PickerView(
        "Resume a conversation",
        tuple(
            PickerRow(item.session_id, *render_session_parts(item)) for item in sessions
        ),
        "↑↓ select · Enter resume · Esc cancel",
    )


def provider_select_panel(
    providers: tuple[ProviderView, ...],
) -> PickerView:
    rows = tuple(
        PickerRow(
            f"provider:{item.id}",
            f"{'● ' if item.active else ''}{item.id} · {item.model}",
        )
        for item in providers
    )
    return PickerView(
        "Choose a provider",
        (*rows, PickerRow("add", "+ Add provider")),
        "↑↓ navigate · Enter select · Esc close",
    )


def provider_actions_panel(provider: ProviderView) -> PickerView:
    actions = [
        PickerRow("use", "Use this provider"),
        PickerRow("configure", "Configure"),
    ]
    if provider.has_stored_key:
        actions.append(PickerRow("remove", "Remove saved API key"))
    actions.append(PickerRow("back", "Back"))
    detail = (
        f"{provider.id} · {provider.protocol.value} · {provider.model}\n"
        f"{provider.base_url or 'SDK default URL'} · "
        f"{_credential_label(provider)}"
    )
    return PickerView(detail, tuple(actions), "↑↓ navigate · Enter select · Esc back")


def provider_remove_credential_panel(
    provider: ProviderView,
) -> PickerView:
    return PickerView(
        f"Remove saved API key for {provider.id!r}?\n"
        "Environment credentials are not affected.",
        (
            PickerRow("remove", "Remove saved API key"),
            PickerRow("cancel", "Cancel"),
        ),
        "↑↓ navigate · Enter confirm · Esc cancel",
    )


def provider_form_panel(
    form: ProviderForm,
    fields: tuple[tuple[str, str], ...],
    field_index: int,
    buffer_text: str,
    discovered_models: tuple[str, ...],
) -> FormattedText:
    name, label = fields[field_index]
    field_value = (
        getattr(form, name) if name != "api_key" else "••••" if buffer_text else ""
    )
    discovered = (
        "\nDiscovered: " + ", ".join(discovered_models[:6]) if discovered_models else ""
    )
    return FormattedText(
        [
            ("class:heading", "Configure provider"),
            ("class:secondary", f" · {field_index + 1}/{len(fields)}\n"),
            ("", f"{label}: {field_value}{discovered}\n"),
            ("class:secondary", "Enter next · Esc back"),
        ]
    )


def provider_review_panel(form: ProviderForm) -> PickerView:
    actions = (
        PickerRow("save", "Save & use"),
        PickerRow("discover", "Discover models"),
        PickerRow("advanced", "Advanced settings"),
        PickerRow("back", "Back to profiles"),
    )
    detail = (
        "Review provider\n"
        f"{form.provider_id or '<provider id>'} · "
        f"{form.protocol} · {form.model or '<model>'}\n"
        f"{form.base_url or 'SDK default URL'} · "
        f"{'new key entered' if form.api_key else 'keep saved key'}"
    )
    return PickerView(detail, actions, "↑↓ navigate · Enter select · Esc cancel")


def provider_models_panel(models: tuple[str, ...]) -> PickerView:
    return PickerView(
        "Choose a model",
        tuple(PickerRow(model, model) for model in models),
        "↑↓ navigate · Enter select · Esc back",
    )


def _message(title: str, hint: str) -> FormattedText:
    return FormattedText([("class:heading", title), ("class:secondary", f" · {hint}")])


def _credential_label(provider: ProviderView) -> str:
    if provider.credential_source.value == "environment":
        return "environment"
    if provider.credential_source.value == "stored":
        return "stored"
    return "not configured"


def render_picker(
    view: PickerView, state: PickerState, width: int | None = None
) -> FormattedText:
    start, rows = state.visible(view.rows, view.visible_count)
    fragments: list[tuple[str, str]] = [("class:heading", view.title), ("", "\n")]
    for offset, row in enumerate(rows):
        index = start + offset
        style = "class:selected" if index == state.index else ""
        if row.trailing is None or width is None:
            fragments.append((style, f"  {row.label}\n"))
            continue
        trailing_width = get_cwidth(row.trailing)
        label_width = max(1, width - trailing_width - 3)
        label = _truncate(row.label, label_width)
        padding = max(1, width - get_cwidth(label) - trailing_width - 2)
        trailing_style = style or "class:secondary"
        fragments.extend(
            (
                (style, f"  {label}"),
                (style, " " * padding),
                (trailing_style, row.trailing),
                ("", "\n"),
            )
        )
    hint = view.hint
    if len(view.rows) > view.visible_count:
        hint += f" · {state.index + 1}/{len(view.rows)}"
    fragments.append(("class:secondary", hint))
    return FormattedText(fragments)


def _truncate(value: str, width: int) -> str:
    if get_cwidth(value) <= width:
        return value
    if width <= 1:
        return "…"[:width]
    available = width - 1
    result = ""
    for character in value:
        if get_cwidth(result + character) > available:
            break
        result += character
    return result + "…"


__all__ = [
    "agent_select_panel",
    "full_access_panel",
    "permission_panel",
    "provider_actions_panel",
    "provider_form_panel",
    "provider_models_panel",
    "provider_remove_credential_panel",
    "provider_review_panel",
    "provider_select_panel",
    "render_picker",
    "resume_panel",
]
