"""从持久化会话消息生成协议安全的模型消息。"""

from dataclasses import replace

from nano_code.context.models import ModelContentBlock, ModelMessage
from nano_code.messages import (
    ChatMessage,
    ContentBlock,
    SystemContextBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from nano_code.prompts.rendering import render_system_context


class ModelMessageProjector:
    """移除本地身份信息、合并同角色消息并验证工具协议。"""

    def project(self, messages: tuple[ChatMessage, ...]) -> tuple[ModelMessage, ...]:
        projected: list[ModelMessage] = []
        for message in messages:
            # 展示快照属于 Transcript/UI，不进入模型可见的领域投影。
            content = tuple(_project_block(block) for block in message.content)
            candidate = ModelMessage(role=message.role, content=content)
            if projected and projected[-1].role == candidate.role:
                previous = projected[-1]
                projected[-1] = ModelMessage(
                    role=previous.role,
                    content=previous.content + candidate.content,
                )
            else:
                projected.append(candidate)

        result = tuple(projected)
        self._validate_tool_pairs(result)
        return result

    @staticmethod
    def _validate_tool_pairs(messages: tuple[ModelMessage, ...]) -> None:
        pending: set[str] = set()
        seen_calls: set[str] = set()
        seen_results: set[str] = set()
        for message in messages:
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    if block.id in seen_calls:
                        raise ValueError(f"Duplicate tool use in context: {block.id}")
                    seen_calls.add(block.id)
                    pending.add(block.id)
                elif isinstance(block, ToolResultBlock):
                    if block.tool_use_id in seen_results:
                        raise ValueError(
                            f"Duplicate tool result in context: {block.tool_use_id}"
                        )
                    if block.tool_use_id not in pending:
                        raise ValueError(
                            f"Orphan tool result in context: {block.tool_use_id}"
                        )
                    seen_results.add(block.tool_use_id)
                    pending.remove(block.tool_use_id)
        if pending:
            unresolved = ", ".join(sorted(pending))
            raise ValueError(f"Unresolved tool use in context: {unresolved}")


def _project_block(block: ContentBlock) -> ModelContentBlock:
    """在唯一边界移除本地展示数据并渲染可信上下文。"""

    if isinstance(block, SystemContextBlock):
        return TextBlock(render_system_context(block))
    if isinstance(block, ToolResultBlock):
        return replace(block, presentation=None)
    return block
