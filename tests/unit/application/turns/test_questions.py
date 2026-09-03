import json

import pytest

from my_code.application.turns.questions import DeferredQuestionBroker, QuestionTool
from my_code.foundation.json import to_json_object
from my_code.tools.base import ToolInputError


def test_question_definition_explains_recommended_suffix() -> None:
    tool = QuestionTool(DeferredQuestionBroker())

    schema = json.dumps(tool.definition.input_schema)

    expected = (
        "Put the recommended option first and suffix its label with "
        '\\"(Recommended)\\".'
    )
    assert expected in schema


def test_question_rejects_recommended_prefix_with_actionable_error() -> None:
    tool = QuestionTool(DeferredQuestionBroker())
    tool_input = to_json_object(
        {
            "questions": [
                {
                    "question": "Which API should be public?",
                    "header": "API",
                    "id": "public_api",
                    "options": [
                        {
                            "label": "(Recommended) Narrow",
                            "description": "Expose only the use case.",
                        },
                        {"label": "Broad", "description": "Expose all models."},
                    ],
                }
            ]
        }
    )

    with pytest.raises(ToolInputError) as error:
        tool.validate_input(tool_input)

    assert str(error.value) == (
        "the first option label must end with (Recommended), "
        "for example: Narrow (Recommended)"
    )
