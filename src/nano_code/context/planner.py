"""从会话快照生成单次模型请求上下文。"""

import json

from nano_code.context.microcompact import (
    MicrocompactPolicy,
    apply_content_replacements,
)
from nano_code.context.models import (
    ContentReplacement,
    ContextBudget,
    ContextPlan,
    ConversationSnapshot,
    ModelMessage,
)
from nano_code.context.projection import ModelMessageProjector
from nano_code.context.window import ContextWindow
from nano_code.messages import (
    ChatMessage,
    SystemContextBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from nano_code.prompts import PromptRegistry, SystemPrompt
from nano_code.tools.base import ToolDefinition


class ContextPlanner:
    """集中编排 prompt、工具和消息投影，不修改会话事实。"""

    def __init__(
        self,
        *,
        window: ContextWindow,
        prompt: PromptRegistry,
        tools: tuple[ToolDefinition, ...],
        max_output_tokens: int,
        projector: ModelMessageProjector | None = None,
        microcompact: MicrocompactPolicy | None = None,
    ) -> None:
        if max_output_tokens < 1:
            raise ValueError("max_output_tokens must be positive")
        self.window = window
        self.prompt = prompt
        self.tools = tools
        self.max_output_tokens = max_output_tokens
        self.projector = projector or ModelMessageProjector()
        self.microcompact = microcompact or MicrocompactPolicy.for_window(
            window.max_chars
        )

    def plan(self, snapshot: ConversationSnapshot) -> ContextPlan:
        """生成不可变请求计划；当前阶段保持既有窗口选择行为。"""

        effective_messages, proposed = self._effective_messages(snapshot)
        selected = self.window.project(effective_messages)
        messages = self.projector.project(selected)
        system_prompt = self.prompt.resolve()
        budget = self._budget(selected, messages, system_prompt)
        return ContextPlan(
            system_prompt=system_prompt,
            messages=messages,
            tools=self.tools,
            max_output_tokens=self.max_output_tokens,
            budget=budget,
            new_content_replacements=proposed,
        )

    def inspect(self, snapshot: ConversationSnapshot) -> ContextBudget:
        """在不触发窗口错误或持久化决策的情况下返回预算快照。"""

        effective_messages, _ = self._effective_messages(snapshot, propose=False)
        model_messages = self.projector.project(effective_messages)
        system_prompt = self.prompt.resolve()
        return self._budget(effective_messages, model_messages, system_prompt)

    def compaction_view(
        self, snapshot: ConversationSnapshot
    ) -> tuple[tuple[ModelMessage, ...], tuple[ContentReplacement, ...]]:
        """生成不经过窗口限制的摘要输入和待提交替换决策。"""

        effective_messages, proposed = self._effective_messages(snapshot)
        return self.projector.project(effective_messages), proposed

    def _effective_messages(
        self, snapshot: ConversationSnapshot, *, propose: bool = True
    ) -> tuple[tuple[ChatMessage, ...], tuple[ContentReplacement, ...]]:
        proposed = (
            self.microcompact.propose(snapshot.messages, snapshot.content_replacements)
            if propose
            else ()
        )
        replacements = snapshot.content_replacements + proposed
        return (
            apply_content_replacements(snapshot.messages, replacements),
            proposed,
        )

    def _budget(
        self,
        messages: tuple[ChatMessage, ...],
        model_messages: tuple[ModelMessage, ...],
        system_prompt: SystemPrompt,
    ) -> ContextBudget:
        actual_input, incremental_tokens, estimated_input = _estimate_input_tokens(
            messages,
            system_prompt.text,
            self.tools,
        )
        return ContextBudget(
            message_limit_chars=self.window.max_chars,
            message_chars=_message_chars(model_messages),
            system_chars=len(system_prompt.text),
            tool_schema_chars=_tool_schema_chars(self.tools),
            reserved_output_tokens=self.max_output_tokens,
            last_actual_input_tokens=actual_input,
            incremental_tokens=incremental_tokens,
            estimated_input_tokens=estimated_input,
        )


def _message_chars(messages: tuple[ModelMessage, ...]) -> int:
    """估算投影消息字符量；后续阶段会替换为 usage 驱动的 token 预算。"""

    size = 0
    for message in messages:
        for block in message.content:
            if isinstance(block, TextBlock):
                size += len(block.text)
            elif isinstance(block, ToolUseBlock):
                size += len(block.name) + len(str(block.input))
            elif isinstance(block, ToolResultBlock):
                size += len(block.content)
    return size


def _tool_schema_chars(tools: tuple[ToolDefinition, ...]) -> int:
    return sum(
        len(
            json.dumps(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        for tool in tools
    )


def _estimate_input_tokens(
    messages: tuple[ChatMessage, ...],
    system_prompt: str,
    tools: tuple[ToolDefinition, ...],
) -> tuple[int | None, int, int]:
    """使用最近真实 usage，并只估算该响应之后新增的上下文。"""

    for index in range(len(messages) - 1, -1, -1):
        usage = messages[index].usage
        if usage is None:
            continue
        incremental_chars = _chat_message_chars(messages[index + 1 :])
        incremental_tokens = _chars_to_tokens(incremental_chars)
        actual_input = usage.total_input_tokens
        estimated = actual_input + usage.output_tokens + incremental_tokens
        return actual_input, incremental_tokens, estimated

    total_chars = (
        len(system_prompt) + _tool_schema_chars(tools) + _chat_message_chars(messages)
    )
    estimated = _chars_to_tokens(total_chars)
    return None, estimated, estimated


def _chat_message_chars(messages: tuple[ChatMessage, ...]) -> int:
    return sum(_block_chars(block) for message in messages for block in message.content)


def _block_chars(
    block: TextBlock | SystemContextBlock | ToolUseBlock | ToolResultBlock,
) -> int:
    if isinstance(block, TextBlock):
        return len(block.text)
    if isinstance(block, ToolUseBlock):
        return len(block.name) + len(str(block.input))
    if isinstance(block, SystemContextBlock):
        return len(block.content)
    return len(block.content)


def _chars_to_tokens(chars: int) -> int:
    # provider usage 是规范锚点；锚点之后的内容使用保守、可替换的近似值。
    return (chars + 3) // 4
