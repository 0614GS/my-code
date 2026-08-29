"""一次 Agent invocation 的输入、steering source 与终态值。"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from my_code.conversation.attachments import AttachmentPayload
from my_code.model.primitives import TokenUsage


@dataclass(frozen=True, slots=True)
class UserTurnInput:
    """一次用户回合及其在提交前已准备好的事件 attachment。"""

    prompt: str
    attachments: tuple[AttachmentPayload, ...] = ()
    input_id: str | None = None

    def __post_init__(self) -> None:
        if not self.prompt.strip():
            raise ValueError("Prompt must not be empty")


# Keep the original public name for headless callers.  An invocation now accepts
# a batch of these values, but a single value remains source compatible.
AgentTurnInput = UserTurnInput


class PendingInputSource(Protocol):
    """Host-owned, session-bound input queue consumed only at safe boundaries."""

    async def drain_pending(self) -> tuple[UserTurnInput, ...]: ...

    def accept_pending(self, input_ids: Sequence[str]) -> None: ...


@dataclass(frozen=True, slots=True)
class AgentTurnSucceeded:
    """一次用户提示正常完成后的终态数据。"""

    text: str
    completed_steps: int
    usage: TokenUsage


@dataclass(frozen=True, slots=True)
class AgentMaxStepsReached:
    """显式 Step 上限终止了当前用户回合。"""

    max_steps: int
    completed_steps: int
    usage: TokenUsage


type AgentTurnOutcome = AgentTurnSucceeded | AgentMaxStepsReached


__all__ = [
    "AgentMaxStepsReached",
    "AgentTurnInput",
    "AgentTurnOutcome",
    "AgentTurnSucceeded",
    "PendingInputSource",
    "UserTurnInput",
]
