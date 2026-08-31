"""Agent event to host event projection for one explicit Session."""

from collections.abc import AsyncIterator, Callable

from my_code.agent.events import (
    AgentCompactionCompleted,
    AgentCompactionStarted,
    AgentConversationUpdated,
    AgentEvent,
    AgentInputAccepted,
    AgentInputFailed,
    AgentModelRequestPrepared,
    AgentModelStepCompleted,
    AgentPlanCompleted,
    AgentPlanDelta,
    AgentPlanStarted,
    AgentReasoningCompleted,
    AgentReasoningDelta,
    AgentReasoningStarted,
    AgentTextCompleted,
    AgentTextDelta,
    AgentTextStarted,
    AgentToolFinished,
    AgentToolStarted,
)
from my_code.agent.models import AgentInvocationSucceeded, AgentMaxStepsReached
from my_code.application.contracts.events import (
    CompactionCompleted,
    CompactionStarted,
    ContextUpdated,
    MaxStepsReached,
    ModelRequestPrepared,
    ModelStepCompleted,
    PlanCompleted,
    PlanDelta,
    PlanStarted,
    PreparedContext,
    ReasoningCompleted,
    ReasoningDelta,
    ReasoningStarted,
    TextCompleted,
    TextDelta,
    TextStarted,
    TodoListUpdated,
    ToolFinished,
    ToolStarted,
    TurnEvent,
    TurnInputAccepted,
    TurnInputFailed,
    TurnSucceeded,
)
from my_code.application.contracts.status import ContextUsageView
from my_code.features.todos.projection import project_todos
from my_code.sessions.session import Session


async def project_agent_events(
    session: Session,
    events: AsyncIterator[AgentEvent],
    context_status: Callable[[], ContextUsageView],
) -> AsyncIterator[TurnEvent]:
    previous_todo_write_id = project_todos(session.conversation).latest_write_id
    last_input_context_counts: tuple[int, int] | None = None
    async for event in events:
        if isinstance(event, AgentCompactionStarted):
            yield CompactionStarted(event.trigger)
        elif isinstance(event, AgentCompactionCompleted):
            yield CompactionCompleted(event.trigger, event.usage, context_status())
        elif isinstance(event, AgentModelRequestPrepared):
            yield ModelRequestPrepared(
                event.request_id,
                event.request_number,
                event.purpose,
                tuple(
                    PreparedContext(
                        item.audit_id,
                        item.source,
                        item.attachment_kind,
                        item.text,
                    )
                    for item in event.injections
                ),
            )
        elif isinstance(event, AgentInputAccepted):
            yield TurnInputAccepted(event.input_id, event.prompt)
            counts = (session.context_entry_count, session.conversation_entry_count)
            if counts != last_input_context_counts:
                last_input_context_counts = counts
                yield ContextUpdated(context_status())
        elif isinstance(event, AgentInputFailed):
            yield TurnInputFailed(event.input_id, event.prompt, event.error)
        elif isinstance(event, AgentTextStarted):
            yield TextStarted()
        elif isinstance(event, AgentTextDelta):
            yield TextDelta(event.text)
        elif isinstance(event, AgentTextCompleted):
            yield TextCompleted(event.text)
        elif isinstance(event, AgentPlanStarted):
            yield PlanStarted()
        elif isinstance(event, AgentPlanDelta):
            yield PlanDelta(event.text)
        elif isinstance(event, AgentPlanCompleted):
            yield PlanCompleted(event.plan)
        elif isinstance(event, AgentReasoningStarted):
            yield ReasoningStarted(event.disclosure)
        elif isinstance(event, AgentReasoningDelta):
            yield ReasoningDelta(event.disclosure, event.part_index, event.text)
        elif isinstance(event, AgentReasoningCompleted):
            yield ReasoningCompleted(event.presentation)
        elif isinstance(event, AgentModelStepCompleted):
            yield ModelStepCompleted(event.step_index, event.has_tools)
        elif isinstance(event, AgentToolStarted):
            yield ToolStarted(
                event.tool_use_id, event.presentation, event.name, event.input
            )
        elif isinstance(event, AgentToolFinished):
            yield ToolFinished(event.tool_use_id, event.is_error, event.presentation)
        elif isinstance(event, AgentConversationUpdated):
            todo_projection = project_todos(session.conversation)
            if todo_projection.latest_write_id != previous_todo_write_id:
                previous_todo_write_id = todo_projection.latest_write_id
                if todo_projection.latest_write_todos is not None:
                    yield TodoListUpdated(todo_projection.latest_write_todos)
            yield ContextUpdated(context_status())
        elif isinstance(event, AgentInvocationSucceeded):
            yield TurnSucceeded(
                event.text,
                event.completed_steps,
                event.usage.input_tokens,
                event.usage.output_tokens,
            )
        elif isinstance(event, AgentMaxStepsReached):
            yield MaxStepsReached(
                event.max_steps,
                event.completed_steps,
                event.usage.input_tokens,
                event.usage.output_tokens,
            )


__all__ = ["project_agent_events"]
