"""Provider-neutral tool discovery and dispatcher behavior."""

from pathlib import Path

import pytest

from my_code.context.attachments.projection import AttachmentProjector
from my_code.conversation.attachments import ToolDiscoveryAttachment
from my_code.conversation.models import ToolCall
from my_code.foundation.json import JsonObject
from my_code.model.request import InputText, ModelToolDefinition
from my_code.model.tool_search import ToolSearchMode
from my_code.permissions.models import (
    PermissionDecisionKind,
    PermissionDecisionReason,
    PermissionMode,
    ToolPermissionContext,
    ToolPermissionResult,
)
from my_code.permissions.policy import PermissionPolicy
from my_code.permissions.prompt import HeadlessPrompter
from my_code.tools.base import Tool, ToolContext, ToolExposure, ToolOutput
from my_code.tools.catalog import ToolCatalogSnapshot
from my_code.tools.discovery import (
    ToolExposureSnapshot,
    discovery_definition,
)
from my_code.tools.executor import ToolExecutor
from my_code.tools.search import InvokeSearchedTool, ToolSearch
from my_code.workspace.local import Workspace


class SearchableTool(Tool):
    def __init__(self, name: str = "Hidden", description: str = "find records") -> None:
        self._definition = ModelToolDefinition(
            name,
            description,
            {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        )
        self.inputs: list[JsonObject] = []

    @property
    def definition(self) -> ModelToolDefinition:
        return self._definition

    @property
    def exposure(self) -> ToolExposure:
        return ToolExposure.SEARCHABLE

    def is_read_only(self, tool_input: JsonObject, context: ToolContext) -> bool:
        del tool_input, context
        return False

    async def check_permissions(
        self, tool_input: JsonObject, context: ToolPermissionContext
    ) -> ToolPermissionResult:
        del context
        return ToolPermissionResult.allow(
            tool_input,
            message="test",
            reason=PermissionDecisionReason(PermissionDecisionKind.TOOL, "test"),
        )

    def validate_input(self, tool_input: JsonObject) -> None:
        if set(tool_input) != {"value"} or not isinstance(tool_input.get("value"), str):
            raise ValueError("value must be a string")

    async def execute(self, tool_input: JsonObject, context: ToolContext) -> ToolOutput:
        del context
        self.inputs.append(tool_input)
        return ToolOutput(f"target:{tool_input['value']}")


def catalog(mode: ToolSearchMode) -> tuple[ToolCatalogSnapshot, SearchableTool]:
    hidden = SearchableTool()
    tools: tuple[Tool, ...] = (ToolSearch(mode), hidden)
    if mode is ToolSearchMode.DISPATCHER:
        tools = (*tools, InvokeSearchedTool())
    return ToolCatalogSnapshot.from_tools(tools), hidden


def test_tool_search_describes_the_active_routing_protocol() -> None:
    dispatcher = ToolSearch(ToolSearchMode.DISPATCHER).definition.description
    native = ToolSearch(ToolSearchMode.NATIVE).definition.description
    invoker = InvokeSearchedTool().definition.description

    assert "only through InvokeSearchedTool" in dispatcher
    assert "call matches directly starting next step" in native
    assert "Never call searched tools directly" in invoker


def test_dispatcher_discovery_projects_schema_with_strict_route() -> None:
    definition = discovery_definition(SearchableTool())
    projected = AttachmentProjector().project(
        ToolDiscoveryAttachment((definition,), "dispatcher")
    )
    content = projected.content[0]
    assert isinstance(content, InputText)
    text = content.text

    assert "DISPATCHER RULE: Never call discovered tools directly" in text
    assert '"name": "Hidden"' in text
    assert '"input_schema"' in text


def test_native_discovery_does_not_require_dispatcher() -> None:
    definition = discovery_definition(SearchableTool())
    projected = AttachmentProjector().project(
        ToolDiscoveryAttachment((definition,), "native")
    )
    content = projected.content[0]
    assert isinstance(content, InputText)
    text = content.text

    assert "available as native tools" in text
    assert "InvokeSearchedTool" not in text


@pytest.mark.asyncio
async def test_search_is_available_next_step_and_dispatches_target(
    tmp_path: Path,
) -> None:
    snapshot, hidden = catalog(ToolSearchMode.DISPATCHER)
    before = ToolExposureSnapshot.build(snapshot, ToolSearchMode.DISPATCHER, {})
    executor = ToolExecutor(
        snapshot,
        PermissionPolicy(PermissionMode.DEFAULT),
        HeadlessPrompter(),
        Workspace(tmp_path),
    )

    direct = await executor.execute(
        ToolCall("direct", "Hidden", {"value": "no"}), tools=before
    )
    early = await executor.execute(
        ToolCall(
            "early",
            "InvokeSearchedTool",
            {"tool_name": "Hidden", "arguments": {"value": "no"}},
        ),
        tools=before,
    )
    searched = await executor.execute(
        ToolCall("search", "ToolSearch", {"query": "select:Hidden"}),
        tools=before,
    )

    assert direct.result.is_error and early.result.is_error
    assert "use ToolSearch first" in direct.result.content
    assert hidden.inputs == []
    attachment = searched.new_attachments[0]
    assert isinstance(attachment, ToolDiscoveryAttachment)
    after = ToolExposureSnapshot.build(
        snapshot,
        ToolSearchMode.DISPATCHER,
        {item.name: item for item in attachment.definitions},
    )
    assert before.definitions == after.definitions

    mistaken_direct = await executor.execute(
        ToolCall("mistaken-direct", "Hidden", {"value": "not-run"}), tools=after
    )

    assert mistaken_direct.result.is_error
    assert "already discovered" in mistaken_direct.result.content
    assert "Retry with InvokeSearchedTool" in mistaken_direct.result.content
    assert "Do not call ToolSearch again" in mistaken_direct.result.content
    assert mistaken_direct.new_attachments == ()
    assert mistaken_direct.permission_updates == ()
    assert hidden.inputs == []

    invoked = await executor.execute(
        ToolCall(
            "invoke",
            "InvokeSearchedTool",
            {"tool_name": "Hidden", "arguments": {"value": "yes"}},
        ),
        tools=after,
    )
    assert invoked.result.content == "target:yes"
    assert hidden.inputs == [{"value": "yes"}]


def test_native_appends_valid_searched_definitions_after_eager_prefix() -> None:
    snapshot, hidden = catalog(ToolSearchMode.NATIVE)
    before = ToolExposureSnapshot.build(snapshot, ToolSearchMode.NATIVE, {})
    after = ToolExposureSnapshot.build(
        snapshot,
        ToolSearchMode.NATIVE,
        {"Hidden": discovery_definition(hidden)},
    )

    assert [item.name for item in before.definitions] == ["ToolSearch"]
    assert [item.name for item in after.definitions] == ["ToolSearch", "Hidden"]


def test_changed_definition_invalidates_discovery() -> None:
    snapshot, hidden = catalog(ToolSearchMode.DISPATCHER)
    discovery = discovery_definition(hidden)
    changed = ToolCatalogSnapshot.from_tools(
        (
            ToolSearch(ToolSearchMode.DISPATCHER),
            InvokeSearchedTool(),
            SearchableTool(description="changed"),
        )
    )
    exposure = ToolExposureSnapshot.build(
        changed, ToolSearchMode.DISPATCHER, {"Hidden": discovery}
    )

    assert exposure.invalidated({"Hidden": discovery}) == ("Hidden",)
    assert "Hidden" not in exposure.searched
