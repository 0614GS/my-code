"""Session listing and validated restore candidate construction."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event as ThreadEvent
from threading import Thread
from uuid import uuid4

from my_code.application.contracts.history import HistoryEntry
from my_code.application.sessions.history_projection import project_history
from my_code.config.paths import MyCodePaths
from my_code.conversation.attachments import PlanHandoffAttachment
from my_code.model.tool_search import ToolSearchMode
from my_code.permissions.models import PermissionMode, PermissionRule
from my_code.permissions.policy import PermissionPolicy
from my_code.sessions.catalog import SessionCatalog, SessionSummary
from my_code.sessions.models import CollaborationMode
from my_code.sessions.session import Session
from my_code.skills.tool import restore_skill_permissions
from my_code.tools.catalog import ToolCatalogSnapshot
from my_code.tools.executor import ToolExecutor


@dataclass(frozen=True, slots=True)
class SessionRestoreCandidate:
    session: Session
    permission_policy: PermissionPolicy
    history: tuple[HistoryEntry, ...]


class SessionOperations:
    def __init__(
        self,
        project_state_dir: Path,
        paths: MyCodePaths,
        tool_executor: ToolExecutor,
        tool_search_mode: ToolSearchMode,
    ) -> None:
        self._project_state_dir = project_state_dir
        self._paths = paths
        self._tool_executor = tool_executor
        self._tool_search_mode = tool_search_mode

    async def list(self, current_session_id: str) -> tuple[SessionSummary, ...]:
        catalog = SessionCatalog(self._project_state_dir)
        return await _offload_session_io(
            lambda: catalog.list(exclude_session_id=current_session_id)
        )

    async def restore(
        self,
        session_id: str,
        *,
        permission_rules: tuple[PermissionRule, ...],
        tools: ToolCatalogSnapshot,
    ) -> SessionRestoreCandidate:
        session = await _offload_session_io(
            lambda: Session.restore(
                self._project_state_dir,
                session_id,
                tool_results_dir=self._paths.tool_results_dir(session_id),
            )
        )
        history = await _offload_session_io(
            lambda: project_history(
                session,
                catalog=tools,
                search_mode=self._tool_search_mode,
                tool_executor=self._tool_executor,
            )
        )
        permission_mode = (
            PermissionMode.PLAN
            if CollaborationMode(session.collaboration_mode) is CollaborationMode.PLAN
            else PermissionMode(session.permission_mode)
        )
        policy = PermissionPolicy(permission_mode, permission_rules)
        restore_skill_permissions(policy, session.conversation)
        return SessionRestoreCandidate(session, policy, history)

    def create_plan_handoff(
        self,
        previous: Session,
        plan: str,
        *,
        permission_rules: tuple[PermissionRule, ...],
    ) -> tuple[Session, PermissionPolicy]:
        session_id = str(uuid4())
        start = replace(
            previous.start,
            session_id=session_id,
            created_at=datetime.now(UTC).isoformat(),
            permission_mode=previous.permission_mode,
            collaboration_mode=CollaborationMode.DEFAULT.value,
        )
        session = Session(
            self._project_state_dir,
            session_id,
            tool_results_dir=self._paths.tool_results_dir(session_id),
            start=start,
        )
        session.append_attachment(PlanHandoffAttachment(plan))
        return session, PermissionPolicy(
            PermissionMode(previous.permission_mode), permission_rules
        )


async def _offload_session_io[T](operation: Callable[[], T]) -> T:
    done = ThreadEvent()
    result: list[T] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            result.append(operation())
        except BaseException as error:
            errors.append(error)
        finally:
            done.set()

    worker = Thread(target=run, name="my-code-session-io", daemon=True)
    worker.start()
    while not done.is_set():  # noqa: ASYNC110 - threading.Event is cross-thread
        await asyncio.sleep(0.001)
    worker.join()
    if errors:
        raise errors[0]
    return result[0]


__all__ = ["SessionOperations", "SessionRestoreCandidate"]
