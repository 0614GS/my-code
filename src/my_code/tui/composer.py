"""Composer completion adapter for slash commands and path mentions."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Protocol

from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.layout.processors import (
    Processor,
    Transformation,
    TransformationInput,
)

from my_code.features.file_mentions.models import PathSuggestion
from my_code.tui.commands import SlashCommand, SlashCommandRegistry
from my_code.tui.completion import format_path_mention, mention_at_cursor
from my_code.tui.picker import PickerRow, PickerState


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


class ContinuationIndent(Processor):
    """Align logical continuation lines with text after the composer prompt."""

    def __init__(self, width: int) -> None:
        self.width = width

    def apply_transformation(
        self, transformation_input: TransformationInput
    ) -> Transformation:
        if transformation_input.lineno == 0:
            return Transformation(transformation_input.fragments)
        return Transformation(
            [("", " " * self.width), *transformation_input.fragments],
            source_to_display=lambda index: index + self.width,
            display_to_source=lambda index: index - self.width,
        )


@dataclass(slots=True)
class SlashMenuState:
    """Selection state kept separate from prompt_toolkit's text-preview menu."""

    matches: tuple[SlashCommand, ...] = ()
    dismissed_text: str | None = None
    picker: PickerState = field(default_factory=PickerState)

    @property
    def selected(self) -> int:
        return self.picker.index

    def update(self, text: str, registry: SlashCommandRegistry) -> None:
        if (
            not text.startswith("/")
            or any(character.isspace() for character in text)
            or text == self.dismissed_text
        ):
            self.matches = ()
            self.picker.reset()
            return
        matches = registry.matching(text)
        if matches != self.matches:
            self.matches = matches
            self.picker.reset()
        elif matches:
            self.picker.sync(self._rows())

    @property
    def current(self) -> SlashCommand | None:
        if not self.matches:
            return None
        self.picker.sync(self._rows())
        return self.matches[self.picker.index]

    def move(self, offset: int) -> None:
        self.picker.move(self._rows(), offset)

    def visible(self, limit: int = 7) -> tuple[int, tuple[SlashCommand, ...]]:
        start, rows = self.picker.visible(self._rows(), limit)
        return start, self.matches[start : start + len(rows)]

    def dismiss(self, text: str) -> None:
        self.dismissed_text = text
        self.matches = ()
        self.picker.reset()

    def _rows(self) -> tuple[PickerRow, ...]:
        return tuple(PickerRow(command.name, command.name) for command in self.matches)


__all__ = ["ComposerCompleter", "ContinuationIndent", "SlashMenuState"]
