"""Turn execution, steering input, attachment, and cancellation coordination."""

import asyncio
from collections.abc import AsyncIterator, Callable

from my_code.agent.events import AgentEvent
from my_code.agent.models import (
    AgentInvocationSucceeded,
    AgentMaxStepsReached,
    AgentTurnInput,
)
from my_code.agent.runner import InteractiveAgentRunner
from my_code.application.contracts.events import (
    AttachmentLoaded,
    InvocationOutcome,
    MaxStepsReached,
    TurnEvent,
    TurnInputFailed,
    TurnSucceeded,
)
from my_code.application.contracts.inputs import QueuedInputView
from my_code.application.contracts.permissions import PermissionHandler
from my_code.application.contracts.questions import QuestionHandler
from my_code.application.contracts.status import ContextUsageView
from my_code.application.turns.event_projection import project_agent_events
from my_code.application.turns.mentions.loader import AttachmentLoader
from my_code.application.turns.pending_inputs import PendingInputController
from my_code.application.turns.permission_prompt import DeferredPermissionPrompter
from my_code.application.turns.questions import DeferredQuestionBroker
from my_code.context.session_cache import SessionContextCache
from my_code.conversation.attachments import AttachmentPayload
from my_code.sessions.session import Session


class TurnCoordinator:
    def __init__(
        self,
        agent: InteractiveAgentRunner,
        session_id: str,
        attachment_loader: AttachmentLoader | None,
        permission_prompter: DeferredPermissionPrompter,
        question_broker: DeferredQuestionBroker,
    ) -> None:
        self._agent = agent
        self._attachment_loader = attachment_loader
        self._permission_prompter = permission_prompter
        self._question_broker = question_broker
        self._pending = PendingInputController(session_id, attachment_loader)
        self._interactive_task: asyncio.Task[object] | None = None

    @property
    def agent(self) -> InteractiveAgentRunner:
        return self._agent

    def replace_agent(self, agent: InteractiveAgentRunner) -> None:
        if self.is_active:
            raise RuntimeError("Cannot replace the agent during an active turn")
        self._agent = agent

    @property
    def is_active(self) -> bool:
        return self._interactive_task is not None

    def rebind_session(self, session_id: str) -> None:
        if self._pending.queued_inputs():
            raise RuntimeError("Cannot rebind turns while inputs are queued")
        self._pending.clear()
        self._pending = PendingInputController(session_id, self._attachment_loader)

    async def submit(
        self, session: Session, runtime: SessionContextCache, prompt: str
    ) -> InvocationOutcome:
        attachments = await self._load_attachments(prompt)
        result = await self._agent.submit(
            session, runtime, AgentTurnInput(prompt, attachments)
        )
        if isinstance(result, AgentInvocationSucceeded):
            return TurnSucceeded(
                result.text,
                result.completed_steps,
                result.usage.input_tokens,
                result.usage.output_tokens,
            )
        assert isinstance(result, AgentMaxStepsReached)
        return MaxStepsReached(
            result.max_steps,
            result.completed_steps,
            result.usage.input_tokens,
            result.usage.output_tokens,
        )

    async def stream(
        self,
        session: Session,
        runtime: SessionContextCache,
        prompt: str,
        context_status: Callable[[], ContextUsageView],
    ) -> AsyncIterator[TurnEvent]:
        loaded = (
            await self._attachment_loader.load(prompt)
            if self._attachment_loader is not None
            else ()
        )
        for item in loaded:
            yield AttachmentLoaded(item.path, item.is_directory, item.display)
        events = self._agent.stream(
            session,
            runtime,
            AgentTurnInput(prompt, tuple(item.attachment for item in loaded)),
        )
        async for event in self._project_with_cancel(session, events, context_status):
            yield event

    async def stream_interactive(
        self,
        session: Session,
        runtime: SessionContextCache,
        context_status: Callable[[], ContextUsageView],
    ) -> AsyncIterator[TurnEvent]:
        self._interactive_task = asyncio.current_task()
        try:
            while self._pending.has_actionable():
                await self._pending.prepare_pending()
                for failure in self._pending.drain_failures():
                    yield TurnInputFailed(
                        failure.input_id,
                        failure.prompt,
                        failure.error or "Attachment preparation failed",
                    )
                if not self._pending.has_actionable():
                    break
                events = self._agent.stream_continuation(
                    session, runtime, pending_source=self._pending
                )
                async for event in self._project_with_cancel(
                    session, events, context_status
                ):
                    yield event
                for failure in self._pending.drain_failures():
                    yield TurnInputFailed(
                        failure.input_id,
                        failure.prompt,
                        failure.error or "Attachment preparation failed",
                    )
        finally:
            self._interactive_task = None

    async def stream_continuation(
        self,
        session: Session,
        runtime: SessionContextCache,
        context_status: Callable[[], ContextUsageView],
    ) -> AsyncIterator[TurnEvent]:
        events = self._agent.stream_continuation(
            session, runtime, pending_source=self._pending
        )
        async for event in self._project_with_cancel(session, events, context_status):
            yield event

    async def _project_with_cancel(
        self,
        session: Session,
        events: AsyncIterator[AgentEvent],
        context_status: Callable[[], ContextUsageView],
    ) -> AsyncIterator[TurnEvent]:
        try:
            async for event in project_agent_events(session, events, context_status):
                yield event
        except asyncio.CancelledError:
            session.close_unresolved_tool_calls(
                "Tool execution was aborted by the user."
            )
            raise

    def queue_input(self, prompt: str) -> QueuedInputView:
        return self._pending.queue_input(prompt)

    def recall_latest_input(self) -> str | None:
        return self._pending.recall_latest_input()

    def queued_inputs(self) -> tuple[QueuedInputView, ...]:
        return self._pending.queued_inputs()

    def cancel_active_turn(self) -> None:
        task = self._interactive_task
        if task is not None and not task.done():
            task.cancel()

    def set_permission_handler(self, handler: PermissionHandler) -> None:
        self._permission_prompter.set_handler(handler)

    def set_question_handler(self, handler: QuestionHandler | None) -> None:
        self._question_broker.set_handler(handler)

    @property
    def question_active(self) -> bool:
        return self._question_broker.is_active

    async def close(self) -> None:
        self._pending.clear()
        await self._permission_prompter.close()
        await self._question_broker.close()

    async def _load_attachments(self, prompt: str) -> tuple[AttachmentPayload, ...]:
        if self._attachment_loader is None:
            return ()
        loaded = await self._attachment_loader.load(prompt)
        return tuple(item.attachment for item in loaded)


__all__ = ["TurnCoordinator"]
