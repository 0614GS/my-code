"""Frontend-neutral deferred interaction values for planning questions."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from my_code.tools.base import ToolExecutionError


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


class DeferredQuestionBroker:
    """Own exactly one interactive Question request at a time."""

    def __init__(self) -> None:
        self._handler: QuestionHandler | None = None
        self._lock = asyncio.Lock()
        self._closed = False
        self._active: asyncio.Task[tuple[QuestionAnswer, ...]] | None = None

    @property
    def has_handler(self) -> bool:
        return self._handler is not None and not self._closed

    @property
    def is_active(self) -> bool:
        return self._active is not None

    def set_handler(self, handler: QuestionHandler | None) -> None:
        if self._closed and handler is not None:
            raise RuntimeError("Question broker is closed")
        self._handler = handler

    async def ask(self, request: QuestionRequest) -> tuple[QuestionAnswer, ...]:
        handler = self._handler
        if self._closed or handler is None:
            raise ToolExecutionError(
                "Question requires an active interactive frontend in Plan mode."
            )
        async with self._lock:

            async def invoke() -> tuple[QuestionAnswer, ...]:
                return await handler(request)

            task = asyncio.create_task(invoke())
            self._active = task
            try:
                answers = tuple(await task)
            finally:
                self._active = None
            expected = tuple(question.id for question in request.questions)
            if tuple(answer.id for answer in answers) != expected:
                raise ToolExecutionError(
                    "Question frontend returned missing or out-of-order answers."
                )
            return answers

    async def close(self) -> None:
        self._closed = True
        self._handler = None
        task = self._active
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


__all__ = [
    "DeferredQuestionBroker",
    "QuestionAnswer",
    "QuestionHandler",
    "QuestionOption",
    "QuestionPrompt",
    "QuestionRequest",
]
