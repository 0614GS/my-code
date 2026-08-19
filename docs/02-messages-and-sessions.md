# 对话消息与会话

## 三个边界

| 数据 | 所有者 | 生命周期 |
| --- | --- | --- |
| `ConversationMessage` 与内容块 | `conversation.models` | 内存中的 canonical facts |
| `Conversation` 与 compact/replacement 状态 | `conversation.state` | 活动 Session |
| JSONL records、codec、store、catalog | `sessions` | 本地持久化 |

`sessions` 可以序列化 Conversation facts，但不拥有消息语义；`model.request` 可以消费投影后的消息，但不拥有历史。

## Conversation

消息使用 UUID、父 UUID 和时间戳建立顺序与父链。当前事实包括：

- `HumanMessage`
- `AssistantMessage`
- `ToolResultsMessage`
- `ConversationSummaryMessage`

Assistant 内容由 text、reasoning 和 tool call 的判别联合组成。ToolResult 只保存模型可见结果；终端展示快照由 Session 单独关联，避免展示规则变化后错误重放历史。

## Session 提交

`sessions.session.Session` 同时更新内存 Conversation 和 `SessionStore`。写入顺序遵循：先验证候选事实，再追加 JSONL，成功后更新内存状态。恢复使用 `sessions.codec` 严格解析 records 并重建 Conversation、compact boundaries、content replacements 和工具展示快照。

JSONL schema 只存在于 `sessions.records`。其他模块不得创建 `.jsonl` 路径、导入 records 或依赖 schema version。

## 恢复

`ChatService.resume_session()` 在锁内完成：

1. 从目标 JSONL 完整恢复 Session。
2. 创建新的 `ContextSession`。
3. 创建目标 session 专属的 `ToolResultStore`。
4. 从事实投影 host history。
5. 原子替换活动 bundle。

任何步骤失败都保留旧 bundle，不出现“Session 已切换但 Context 或工具结果仍属于旧会话”的混合状态。

## 内存与模型上下文

- 完整可恢复历史归 `Session.history`。
- 当前模型工作集归 `Conversation`，可由 compact 边界重建。
- attachment delivery、prompt cache 和 user context cache 归 `ContextSession`，resume 时全部重建。
- 实际 `ModelRequest.messages` 是一次请求的临时值，不写回 Session。
