"""Question tool and deferred answer broker."""

from __future__ import annotations

import asyncio
import json
import re

from my_code.application.contracts.questions import (
    QuestionAnswer,
    QuestionHandler,
    QuestionOption,
    QuestionPrompt,
    QuestionRequest,
)
from my_code.foundation.json import JsonObject
from my_code.model.request import ModelToolDefinition
from my_code.permissions.models import (
    PermissionDecisionKind,
    PermissionDecisionReason,
    PermissionMode,
    ToolPermissionContext,
    ToolPermissionResult,
)
from my_code.tools.base import (
    Tool,
    ToolExecutionContext,
    ToolExecutionError,
    ToolExposure,
    ToolInputError,
    ToolOutput,
)

QUESTION_TOOL_NAME = "Question"
_ID = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")


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


class QuestionTool(Tool):
    """Searchable root-session interaction tool available only in Plan mode."""

    def __init__(self, broker: DeferredQuestionBroker) -> None:
        self.broker = broker

    @property
    def exposure(self) -> ToolExposure:
        return ToolExposure.SEARCHABLE

    @property
    def definition(self) -> ModelToolDefinition:
        option = {
            "type": "object",
            "properties": {
                "label": {"type": "string", "minLength": 1, "maxLength": 80},
                "description": {"type": "string", "minLength": 1},
            },
            "required": ["label", "description"],
            "additionalProperties": False,
        }
        return ModelToolDefinition(
            QUESTION_TOOL_NAME,
            "Ask the user one to three material single-choice questions in Plan mode.",
            {
                "type": "object",
                "properties": {
                    "questions": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 3,
                        "items": {
                            "type": "object",
                            "properties": {
                                "question": {"type": "string", "minLength": 1},
                                "header": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 12,
                                },
                                "id": {
                                    "type": "string",
                                    "pattern": "^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$",
                                },
                                "options": {
                                    "type": "array",
                                    "minItems": 2,
                                    "maxItems": 3,
                                    "description": (
                                        "Put the recommended option first and suffix "
                                        'its label with "(Recommended)".'
                                    ),
                                    "items": option,
                                },
                            },
                            "required": ["question", "header", "id", "options"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["questions"],
                "additionalProperties": False,
            },
        )

    def validate_input(self, tool_input: JsonObject) -> None:
        _parse_request(tool_input)

    def is_read_only(
        self, tool_input: JsonObject, context: ToolExecutionContext
    ) -> bool:
        del tool_input, context
        return True

    async def check_permissions(
        self, tool_input: JsonObject, context: ToolPermissionContext
    ) -> ToolPermissionResult:
        if context.mode is not PermissionMode.PLAN:
            return ToolPermissionResult.deny(
                message="Question is available only in Plan mode.",
                reason=PermissionDecisionReason(
                    PermissionDecisionKind.SAFETY, "question-outside-plan"
                ),
            )
        return ToolPermissionResult.allow(
            tool_input,
            message="Interactive planning question is allowed.",
            reason=PermissionDecisionReason(
                PermissionDecisionKind.TOOL, "plan-question"
            ),
        )

    async def execute(
        self, tool_input: JsonObject, context: ToolExecutionContext
    ) -> ToolOutput:
        if context.tool_use_id is None:
            raise ToolExecutionError("Question invocation has no tool-use identity.")
        current_session = context.session_id or context.run_id
        current_root = context.root_session_id or current_session
        if current_session != current_root:
            raise ToolExecutionError(
                "Question is available only to the root foreground session."
            )
        prompts = _parse_request(tool_input)
        answers = await self.broker.ask(
            QuestionRequest(prompts, context.tool_use_id, context.run_id)
        )
        return ToolOutput(
            json.dumps(
                {
                    "answers": [
                        {"id": item.id, "answer": item.answer} for item in answers
                    ]
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )


def _parse_request(tool_input: JsonObject) -> tuple[QuestionPrompt, ...]:
    if set(tool_input) != {"questions"} or not isinstance(
        tool_input.get("questions"), list
    ):
        raise ToolInputError("questions must be the only field and must be an array")
    raw_questions = tool_input["questions"]
    assert isinstance(raw_questions, list)
    if not 1 <= len(raw_questions) <= 3:
        raise ToolInputError("questions must contain one to three items")
    prompts: list[QuestionPrompt] = []
    ids: set[str] = set()
    for raw in raw_questions:
        if not isinstance(raw, dict) or set(raw) != {
            "question",
            "header",
            "id",
            "options",
        }:
            raise ToolInputError("each question has question, header, id, and options")
        question, header, question_id, options = (
            raw["question"],
            raw["header"],
            raw["id"],
            raw["options"],
        )
        if not isinstance(question, str) or not question.strip():
            raise ToolInputError("question must not be empty")
        if not isinstance(header, str) or not header.strip() or len(header) > 12:
            raise ToolInputError("header must contain at most 12 characters")
        if not isinstance(question_id, str) or _ID.fullmatch(question_id) is None:
            raise ToolInputError("id must be stable snake_case")
        if question_id in ids:
            raise ToolInputError("question ids must be unique")
        ids.add(question_id)
        if not isinstance(options, list) or not 2 <= len(options) <= 3:
            raise ToolInputError("each question requires two or three options")
        parsed_options: list[QuestionOption] = []
        for option_index, raw_option in enumerate(options):
            if not isinstance(raw_option, dict) or set(raw_option) != {
                "label",
                "description",
            }:
                raise ToolInputError("each option has label and description")
            label, description = raw_option["label"], raw_option["description"]
            if (
                not isinstance(label, str)
                or not label.strip()
                or label.strip().lower() == "other"
            ):
                raise ToolInputError(
                    "option labels must be non-empty and cannot be Other"
                )
            if not isinstance(description, str) or not description.strip():
                raise ToolInputError("option descriptions must not be empty")
            if option_index == 0 and not label.endswith("(Recommended)"):
                raise ToolInputError(
                    "the first option label must end with (Recommended), "
                    "for example: Narrow (Recommended)"
                )
            parsed_options.append(QuestionOption(label, description))
        prompts.append(
            QuestionPrompt(question, header, question_id, tuple(parsed_options))
        )
    return tuple(prompts)


__all__ = [
    "DeferredQuestionBroker",
    "QuestionTool",
]
