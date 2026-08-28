"""Keyboard routing for the composer and temporary panels."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, Protocol

from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent

from my_code.permissions.models import PermissionConfirmation

_NAVIGATION_PANELS = {
    "full_access",
    "resume",
    "provider_select",
    "provider_actions",
    "provider_remove_credential",
    "provider_review",
    "provider_models",
    "agents",
}
_PROVIDER_PANELS = {
    "provider_actions",
    "provider_remove_credential",
    "provider_form",
    "provider_review",
    "provider_models",
}


class KeyBindingHost(Protocol):
    buffer: Buffer
    _panel: str | None
    _busy: bool
    _mention_span: tuple[int, int] | None
    _path_suggestions: tuple[Any, ...]
    _foreground_task: Any

    def _spawn(self, coroutine: Awaitable[Any]) -> Any: ...

    async def _panel_enter(self) -> None: ...

    async def _submit_buffer(self) -> None: ...

    async def _advance_provider(self, offset: int) -> None: ...

    def _resolve_permission(self, result: PermissionConfirmation) -> None: ...

    def _provider_back(self) -> None: ...

    def _close_panel(self) -> None: ...

    def _move_panel(self, offset: int) -> None: ...

    def _permission_selecting(self) -> bool: ...

    def _choose_permission(self, choice: str) -> None: ...

    def _invalidate(self) -> None: ...

    def _slash_active(self) -> bool: ...

    def _accept_slash(self, *, execute: bool) -> None: ...

    def _move_slash(self, offset: int) -> None: ...

    def _dismiss_slash(self) -> None: ...

    def _cycle_agent_view(self) -> None: ...

    def _cycle_permission_mode(self) -> None: ...

    def _resolve_full_access(self, allow: bool) -> None: ...

    def _scroll_agent(self, offset: int | None) -> None: ...

    async def _open_transcript(self) -> None: ...


def build_key_bindings(host: KeyBindingHost) -> KeyBindings:
    bindings = KeyBindings()

    @bindings.add("enter")
    def enter(event: KeyPressEvent) -> None:
        del event
        if host._panel is not None:
            host._spawn(host._panel_enter())
            return
        if host._slash_active():
            host._accept_slash(execute=True)
            return
        if not host._busy:
            host._spawn(host._submit_buffer())

    @bindings.add("escape", "enter")
    def newline(event: KeyPressEvent) -> None:
        del event
        if host._panel is None and not host._busy:
            host.buffer.insert_text("\n")

    @bindings.add("escape")
    def escape(event: KeyPressEvent) -> None:
        del event
        if host._slash_active():
            host._dismiss_slash()
        elif host.buffer.complete_state is not None:
            host.buffer.cancel_completion()
        elif host._panel == "permission":
            host._resolve_permission(PermissionConfirmation(False))
        elif host._panel == "full_access":
            host._resolve_full_access(False)
        elif host._panel in _PROVIDER_PANELS:
            host._provider_back()
        elif host._panel == "agents":
            host._close_panel()
        elif host._panel is not None:
            host._close_panel()
        elif host._foreground_task is not None:
            host._foreground_task.cancel()

    @bindings.add("tab")
    def tab(event: KeyPressEvent) -> None:
        del event
        if host._panel == "provider_form":
            host._spawn(host._advance_provider(1))
        elif host._slash_active():
            host._accept_slash(execute=False)
        elif host.buffer.complete_state is not None:
            completion = host.buffer.complete_state.current_completion
            if completion is None:
                return
            if host._mention_span is not None and host._path_suggestions:
                start, end = host._mention_span
                replacement = completion.text
                text = host.buffer.text[:start] + replacement + host.buffer.text[end:]
                host.buffer.set_document(
                    Document(text, start + len(replacement)), bypass_readonly=True
                )
            else:
                host.buffer.apply_completion(completion)
        elif host._panel is None and not host._busy:
            host.buffer.start_completion(select_first=True)

    @bindings.add("s-tab")
    def previous_field(event: KeyPressEvent) -> None:
        del event
        if host._panel == "provider_form":
            host._spawn(host._advance_provider(-1))
        elif host._panel is None:
            host._cycle_permission_mode()

    @bindings.add("up")
    def up(event: KeyPressEvent) -> None:
        del event
        if host._permission_selecting():
            host._move_panel(-1)
        elif host._panel in _NAVIGATION_PANELS:
            host._move_panel(-1)
        elif host._slash_active():
            host._move_slash(-1)
        elif host.buffer.complete_state is not None:
            host.buffer.complete_previous()
        else:
            host.buffer.auto_up()

    @bindings.add("down")
    def down(event: KeyPressEvent) -> None:
        del event
        if host._permission_selecting():
            host._move_panel(1)
        elif host._panel in _NAVIGATION_PANELS:
            host._move_panel(1)
        elif host._slash_active():
            host._move_slash(1)
        elif host.buffer.complete_state is not None:
            host.buffer.complete_next()
        else:
            host.buffer.auto_down()

    @bindings.add("c-c")
    def ctrl_c(event: KeyPressEvent) -> None:
        del event
        if not host._busy and host._panel is None:
            host.buffer.reset()

    @bindings.add("c-d")
    def ctrl_d(event: KeyPressEvent) -> None:
        if not host.buffer.text and not host._busy and host._panel is None:
            event.app.exit()
        elif host.buffer.cursor_position < len(host.buffer.text):
            host.buffer.delete()

    @bindings.add("c-t")
    def ctrl_t(event: KeyPressEvent) -> None:
        del event
        host._spawn(host._open_transcript())

    @bindings.add("f6")
    def cycle_agents(event: KeyPressEvent) -> None:
        del event
        if host._panel != "permission":
            host._cycle_agent_view()

    @bindings.add("pageup")
    def agent_page_up(event: KeyPressEvent) -> None:
        del event
        host._scroll_agent(20)

    @bindings.add("pagedown")
    def agent_page_down(event: KeyPressEvent) -> None:
        del event
        host._scroll_agent(-20)

    @bindings.add("end")
    def agent_live_tail(event: KeyPressEvent) -> None:
        del event
        host._scroll_agent(None)

    for key, choice in (
        ("1", "allow"),
        ("2", "second"),
        ("3", "third"),
        ("4", "remember"),
    ):

        @bindings.add(key, filter=Condition(host._permission_selecting))
        def permission_choice(event: KeyPressEvent, choice: str = choice) -> None:
            del event
            host._choose_permission(choice)

    return bindings


__all__ = ["build_key_bindings"]
