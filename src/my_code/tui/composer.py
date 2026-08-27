"""Composer completion adapter for slash commands and path mentions."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document

from my_code.features.file_mentions.models import PathSuggestion
from my_code.tui.commands import SlashCommand, SlashCommandRegistry
from my_code.tui.completion import format_path_mention, mention_at_cursor


class ComposerCompletionState(Protocol):
    commands: SlashCommandRegistry
    _mention_span: tuple[int, int] | None
    _path_suggestions: tuple[PathSuggestion, ...]


class ComposerCompleter(Completer):
    def __init__(self, state: ComposerCompletionState) -> None:
        self.state = state

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterator[Completion]:
        del complete_event
        text = document.text
        mention = mention_at_cursor(text, document.cursor_position)
        if mention is None:
            return
        start, end, _ = mention
        if (start, end) != self.state._mention_span:
            return
        for suggestion in self.state._path_suggestions:
            yield Completion(
                format_path_mention(suggestion.path) + " ",
                start_position=start - document.cursor_position,
                display=suggestion.display,
                display_meta="directory" if suggestion.is_directory else "file",
            )


@dataclass(slots=True)
class SlashMenuState:
    """Selection state kept separate from prompt_toolkit's text-preview menu."""

    matches: tuple[SlashCommand, ...] = ()
    selected: int = 0
    dismissed_text: str | None = None

    def update(self, text: str, registry: SlashCommandRegistry) -> None:
        if (
            not text.startswith("/")
            or any(character.isspace() for character in text)
            or text == self.dismissed_text
        ):
            self.matches = ()
            self.selected = 0
            return
        matches = registry.matching(text)
        if matches != self.matches:
            self.matches = matches
            self.selected = 0
        elif matches:
            self.selected = min(self.selected, len(matches) - 1)

    @property
    def current(self) -> SlashCommand | None:
        if not self.matches:
            return None
        return self.matches[self.selected]

    def move(self, offset: int) -> None:
        if self.matches:
            self.selected = min(max(self.selected + offset, 0), len(self.matches) - 1)

    def dismiss(self, text: str) -> None:
        self.dismissed_text = text
        self.matches = ()
        self.selected = 0


__all__ = ["ComposerCompleter", "SlashMenuState"]
