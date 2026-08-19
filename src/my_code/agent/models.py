"""一次 Agent turn 的输入与终态值。"""

from dataclasses import dataclass

from my_code.context.attachments.models import ContextAttachment
from my_code.model.primitives import TokenUsage


@dataclass(frozen=True, slots=True)
class AgentTurnInput:
    """一次用户回合及其在提交前已准备好的事件 attachment。"""

    prompt: str
    attachments: tuple[ContextAttachment, ...] = ()

    def __post_init__(self) -> None:
        if not self.prompt.strip():
            raise ValueError("Prompt must not be empty")
        if any(
            attachment.retention != "live_session" for attachment in self.attachments
        ):
            raise ValueError("Agent turn attachments must use live_session retention")


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
]
