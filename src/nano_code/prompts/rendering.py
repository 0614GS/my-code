"""把可信内部上下文渲染成模型可读标记。"""

from nano_code.messages import SystemContextBlock

_TAGS = {
    "system_reminder": "system-reminder",
    "conversation_summary": "conversation-summary",
}


def render_system_context(block: SystemContextBlock) -> str:
    """只在模型投影边界生成 XML；Transcript 不保存拼接后的文本。"""

    tag = _TAGS[block.kind]
    # 标记不是安全边界，但要避免内部内容意外提前闭合外层结构。
    content = block.content.replace(f"</{tag}>", f"&lt;/{tag}&gt;")
    return f"<{tag}>\n{content}\n</{tag}>"
