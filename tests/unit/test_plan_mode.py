import json
from pathlib import Path

import pytest

from my_code.agent.collaboration import resolve_mode_prelude
from my_code.application.contracts.questions import QuestionAnswer
from my_code.application.turns.questions import DeferredQuestionBroker, QuestionTool
from my_code.conversation.attachments import (
    CollaborationModeAttachment,
    FileMentionAttachment,
    ToolDiscoveryAttachment,
)
from my_code.conversation.models import AttachmentMessage, HumanMessage, ToolCall
from my_code.conversation.proposed_plan import (
    PlanSegmentKind,
    ProposedPlanParser,
    extract_proposed_plan,
    strip_proposed_plan,
)
from my_code.foundation.json import JsonObject, to_json_object
from my_code.model.tool_search import ToolSearchMode
from my_code.permissions.models import PermissionMode
from my_code.permissions.policy import PermissionPolicy
from my_code.permissions.prompt import HeadlessPrompter
from my_code.sessions.models import CollaborationMode
from my_code.sessions.session import Session
from my_code.tools.catalog import ToolCatalogSnapshot
from my_code.tools.discovery import ToolExposureSnapshot, discovery_definition
from my_code.tools.executor import ToolExecutor
from my_code.tools.search import InvokeSearchedTool
from my_code.workspace.local import Workspace

SESSION_ID = "11111111-1111-1111-1111-111111111111"


def _question_input() -> JsonObject:
    return to_json_object(
        {
            "questions": [
                {
                    "question": "Which API should be public?",
                    "header": "API",
                    "id": "public_api",
                    "options": [
                        {
                            "label": "Narrow (Recommended)",
                            "description": "Expose only the use case.",
                        },
                        {"label": "Broad", "description": "Expose all models."},
                    ],
                }
            ]
        }
    )


def test_collaboration_mode_persists_independently_from_base_permission(
    tmp_path: Path,
) -> None:
    session = Session(tmp_path, SESSION_ID)
    session.set_permission_mode(PermissionMode.ACCEPT_EDITS.value)
    session.set_collaboration_mode(CollaborationMode.PLAN.value)
    session.append_human_message(HumanMessage("plan this"))

    restored = Session(tmp_path, SESSION_ID)

    assert restored.permission_mode == PermissionMode.ACCEPT_EDITS.value
    assert restored.collaboration_mode == CollaborationMode.PLAN.value


def test_plan_prelude_precedes_human_and_request_attachments(tmp_path: Path) -> None:
    broker = DeferredQuestionBroker()
    question = QuestionTool(broker)
    catalog = ToolCatalogSnapshot.from_tools((question, InvokeSearchedTool()))
    session = Session(tmp_path, SESSION_ID)
    session.set_collaboration_mode(CollaborationMode.PLAN.value)
    prelude = resolve_mode_prelude(
        mode=CollaborationMode.PLAN,
        context_entries=session.context_entries,
        catalog=catalog,
        discovery_mode="dispatcher",
    )

    session.commit_user_inputs(
        (("plan this", (FileMentionAttachment("a.py", "body"),)),),
        prelude=prelude,
    )

    entries = session.conversation
    assert isinstance(entries[0], AttachmentMessage)
    assert isinstance(entries[0].payload, CollaborationModeAttachment)
    assert isinstance(entries[1], AttachmentMessage)
    assert isinstance(entries[1].payload, ToolDiscoveryAttachment)
    assert isinstance(entries[2], HumanMessage)
    assert isinstance(entries[3], AttachmentMessage)


def test_quick_mode_round_trip_does_not_create_conversation_facts(
    tmp_path: Path,
) -> None:
    session = Session(tmp_path, SESSION_ID)
    session.set_collaboration_mode(CollaborationMode.PLAN.value)
    session.set_collaboration_mode(CollaborationMode.DEFAULT.value)

    assert session.conversation == ()


@pytest.mark.asyncio
async def test_question_dispatch_preserves_outer_call_id(tmp_path: Path) -> None:
    broker = DeferredQuestionBroker()

    async def answer(request):
        assert request.tool_use_id == "outer"
        assert request.run_id == "root"
        return (QuestionAnswer("public_api", "Narrow (Recommended)"),)

    broker.set_handler(answer)
    question = QuestionTool(broker)
    catalog = ToolCatalogSnapshot.from_tools((question, InvokeSearchedTool()))
    exposure = ToolExposureSnapshot.build(
        catalog,
        ToolSearchMode.DISPATCHER,
        {"Question": discovery_definition(question)},
    )
    executor = ToolExecutor(
        tools=catalog,
        policy=PermissionPolicy(PermissionMode.PLAN),
        prompter=HeadlessPrompter(),
        workspace=Workspace(tmp_path),
    )

    outcome = await executor.execute(
        ToolCall(
            "outer",
            "InvokeSearchedTool",
            to_json_object({"tool_name": "Question", "arguments": _question_input()}),
        ),
        tools=exposure,
        run_id="root",
    )

    assert outcome.result.tool_use_id == "outer"
    assert not outcome.result.is_error
    assert json.loads(outcome.result.content) == {
        "answers": [{"answer": "Narrow (Recommended)", "id": "public_api"}]
    }


@pytest.mark.asyncio
async def test_question_is_closed_with_error_without_frontend(tmp_path: Path) -> None:
    question = QuestionTool(DeferredQuestionBroker())
    catalog = ToolCatalogSnapshot.from_tools((question, InvokeSearchedTool()))
    exposure = ToolExposureSnapshot.build(
        catalog,
        ToolSearchMode.DISPATCHER,
        {"Question": discovery_definition(question)},
    )
    executor = ToolExecutor(
        tools=catalog,
        policy=PermissionPolicy(PermissionMode.PLAN),
        prompter=HeadlessPrompter(),
        workspace=Workspace(tmp_path),
    )

    outcome = await executor.execute(
        ToolCall(
            "outer",
            "InvokeSearchedTool",
            to_json_object({"tool_name": "Question", "arguments": _question_input()}),
        ),
        tools=exposure,
    )

    assert outcome.result.tool_use_id == "outer"
    assert outcome.result.is_error
    assert "interactive frontend" in outcome.result.content


def test_proposed_plan_parser_handles_split_tags_and_missing_close() -> None:
    parser = ProposedPlanParser()
    segments = (
        *parser.feed("Intro\n<proposed_"),
        *parser.feed("plan>\n- one\n"),
        *parser.finish(),
    )

    assert [segment.kind for segment in segments] == [
        PlanSegmentKind.TEXT,
        PlanSegmentKind.START,
        PlanSegmentKind.DELTA,
        PlanSegmentKind.COMPLETED,
    ]
    text = "Intro\n<proposed_plan>\n- one\n</proposed_plan>\nOutro"
    assert strip_proposed_plan(text) == "Intro\nOutro"
    assert extract_proposed_plan(text) == "- one"
