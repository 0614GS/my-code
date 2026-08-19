# 对话消息与会话

## 三个边界

| 数据 | 所有者 | 生命周期 |
| --- | --- | --- |
| provider-neutral entries 与内容块 | `Session`（类型定义在 `conversation.models`） | 活动 Session 的内存 canonical facts |
| working set、compact/replacement、context/replay state | `Session` | 活动 Session |
| `_records`、`_codec`、`_store` 与结果文件 | `sessions` 私有实现 | 本地持久化 |

`sessions` 可以序列化 Conversation facts，但不拥有消息语义；`model.request` 可以消费投影后的消息，但不拥有历史。

## Canonical conversation

消息使用 UUID、父 UUID 和时间戳建立顺序与父链。当前事实包括：

- `HumanMessage`
- `AssistantMessage`
- `ToolResultBatch`
- `ConversationSummaryMessage`

Assistant 内容由 text、reasoning 和 tool call 的判别联合组成。`ToolResultBatch` 是独立 entry，不属于 HumanMessage；它按 `source_assistant_id` 闭合一个 AssistantMessage 中的全部 ToolCall。ToolResult 只保存模型可见结果；终端展示快照由 Session 单独关联。

## Session 提交

`sessions.session.Session` 是唯一公开提交边界。写入顺序遵循：先验证完整候选事务，再通过私有 store 原子替换 JSONL，成功后更新内存状态。工具结果外置、presentation、provider replay sidecar 与 compact 也包含在同一提交语义内；失败时内存和已有 transcript 都不部分推进。

JSONL schema 只存在于 `sessions._records`、`sessions._codec` 和 `sessions._store`。其他生产模块不得导入这些私有模块、创建 session `.jsonl` 路径或依赖 schema version。

## 恢复

`ChatService.resume_session()` 在锁内完成：

1. 构造候选 Session 并从目标 JSONL 完整恢复事实、工作集、presentation 与 replay sidecar。
2. 从候选 Session 的 snapshot 投影 host history。
3. 只有上述步骤全部成功，才通过 `AppState.replace_session()` 原子替换活动引用。

任何步骤失败都保留旧 Session，不出现 conversation、context、工具结果与 replay 分属不同会话的混合状态。Session 的 context delivery/cache 是进程内状态，resume/switch 会随新 Session 清空或重建。

## 内存与模型上下文

- 完整可恢复历史与当前工作集都归 `Session`，调用方只能读取不可变 snapshot。
- attachment delivery、prompt cache 和 user context cache 归 Session 的私有进程内状态。
- replay payload 与 canonical Assistant content 分离，以 entry/content ID 关联。
- `ModelRequest.input` 是一次 step 的 `ModelInputItem` 投影，不写回 Session。
- `ToolOutputs` 与 `UserInput`、`AssistantOutput` 并列；只有 Anthropic adapter 将其包装为 user-role `tool_result` blocks，OpenAI 直接映射顶层 `function_call_output`。
