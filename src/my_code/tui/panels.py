"""Rendering for temporary bottom panels.

Panel navigation and mutations remain in the application controller; this module
only turns safe frontend state into compact terminal text.
"""

from prompt_toolkit.formatted_text import FormattedText

from my_code.chat.permissions import PermissionRequest
from my_code.providers.manager import ProviderView
from my_code.sessions.catalog import SessionSummary
from my_code.tui.provider_screen import ProviderForm
from my_code.tui.resume_screen import render_session


def permission_panel(
    request: PermissionRequest, mode: str, selected: int = 1
) -> FormattedText:
    if mode == "feedback":
        return _message("Permission denied with feedback", "Enter to send · Esc deny")
    if mode == "prefix":
        return _message("Allow Bash prefix", "Enter a rule such as git diff:*")
    choices = ["Yes", "No", "No, with feedback"]
    if request.tool_name == "Bash" or request.suggestions:
        choices.append("Yes, and remember")
    return _picker(
        f"Tool use · {request.presentation.display_name}"
        f" ({request.presentation.summary})\n{request.message}",
        tuple(choices),
        selected,
        "↑↓ select · Enter confirm · 1–4 shortcut · Esc deny",
    )


def resume_panel(sessions: tuple[SessionSummary, ...], selected: int) -> FormattedText:
    if not sessions:
        return _message("No conversations found to resume", "Esc close")
    return _picker(
        "Resume a conversation",
        tuple(render_session(item) for item in sessions[:8]),
        selected,
        "↑↓ select · Enter resume · Esc cancel",
    )


def provider_select_panel(
    providers: tuple[ProviderView, ...], selected: int
) -> FormattedText:
    rows = tuple(
        f"{'● ' if item.active else ''}{item.id} · {item.model}"
        for item in providers[:8]
    )
    return _picker(
        "Choose a provider",
        (*rows, "+ Add provider"),
        selected,
        "↑↓ navigate · Enter select · Esc close",
    )


def provider_actions_panel(provider: ProviderView, selected: int) -> FormattedText:
    actions = ["Use this provider", "Configure"]
    if provider.has_stored_key:
        actions.append("Remove saved API key")
    actions.append("Back")
    detail = (
        f"{provider.id} · {provider.protocol.value} · {provider.model}\n"
        f"{provider.base_url or 'SDK default URL'} · "
        f"{_credential_label(provider)}"
    )
    return _picker(
        detail, tuple(actions), selected, "↑↓ navigate · Enter select · Esc back"
    )


def provider_remove_credential_panel(
    provider: ProviderView, selected: int
) -> FormattedText:
    return _picker(
        f"Remove saved API key for {provider.id!r}?\n"
        "Environment credentials are not affected.",
        ("Remove saved API key", "Cancel"),
        selected,
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


def provider_review_panel(form: ProviderForm, selected: int) -> FormattedText:
    actions = (
        "Save & use",
        "Discover models",
        "Advanced settings",
        "Back to profiles",
    )
    detail = (
        "Review provider\n"
        f"{form.provider_id or '<provider id>'} · "
        f"{form.protocol} · {form.model or '<model>'}\n"
        f"{form.base_url or 'SDK default URL'} · "
        f"{'new key entered' if form.api_key else 'keep saved key'}"
    )
    return _picker(detail, actions, selected, "↑↓ navigate · Enter select · Esc cancel")


def provider_models_panel(models: tuple[str, ...], selected: int) -> FormattedText:
    return _picker(
        "Choose a model",
        models[:8],
        selected,
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


def _picker(
    title: str, rows: tuple[str, ...], selected: int, hint: str
) -> FormattedText:
    fragments: list[tuple[str, str]] = [("class:heading", title), ("", "\n")]
    for index, row in enumerate(rows):
        style = "class:selected" if index == selected else ""
        fragments.append((style, f"  {row}\n"))
    fragments.append(("class:secondary", hint))
    return FormattedText(fragments)


__all__ = [
    "permission_panel",
    "provider_actions_panel",
    "provider_form_panel",
    "provider_models_panel",
    "provider_remove_credential_panel",
    "provider_review_panel",
    "provider_select_panel",
    "resume_panel",
]
