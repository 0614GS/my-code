"""Frontend-neutral question request and answer values."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class QuestionOption:
    label: str
    description: str


@dataclass(frozen=True, slots=True)
class QuestionPrompt:
    question: str
    header: str
    id: str
    options: tuple[QuestionOption, ...]


@dataclass(frozen=True, slots=True)
class QuestionRequest:
    questions: tuple[QuestionPrompt, ...]
    tool_use_id: str
    run_id: str | None


@dataclass(frozen=True, slots=True)
class QuestionAnswer:
    id: str
    answer: str

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.answer.strip():
            raise ValueError("Question answer fields must not be empty")


type QuestionHandler = Callable[
    [QuestionRequest], Awaitable[tuple[QuestionAnswer, ...]]
]


__all__ = [
    "QuestionAnswer",
    "QuestionHandler",
    "QuestionOption",
    "QuestionPrompt",
    "QuestionRequest",
]
