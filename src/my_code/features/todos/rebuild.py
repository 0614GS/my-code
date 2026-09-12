"""从成功 TodoWrite 事实恢复压缩后的当前状态，不把快照视作新写入。"""

from my_code.context.session_cache import CompactionInput
from my_code.conversation.attachments import TodoSnapshotAttachment, TodoSnapshotEntry
from my_code.features.todos.projection import project_todos


class TodoPostCompactAttachmentSource:
    def __call__(self, state: CompactionInput) -> tuple[TodoSnapshotAttachment, ...]:
        projection = project_todos(state.conversation)
        if projection.latest_write_id is None:
            return ()
        return (
            TodoSnapshotAttachment(
                projection.latest_write_id,
                tuple(
                    TodoSnapshotEntry(todo.content, todo.status, todo.active_form)
                    for todo in projection.todos
                ),
            ),
        )


__all__ = ["TodoPostCompactAttachmentSource"]
