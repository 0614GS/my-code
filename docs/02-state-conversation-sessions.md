# 状态、对话与 Session

本专题回答两个问题：一份状态由谁维护，以及它在何时创建、提交、恢复和销毁。

## 状态所有权

| 状态 | 唯一所有者 | 生命周期 | 持久化 |
| --- | --- | --- | --- |
| workspace 与安全边界 | `AppState.workspace` | runtime | 配置单独落盘 |
| 活动 Session 引用 | `AppState` | runtime | 由 Session 负责 |
| conversation、context entries、compact state | `Session` | session | 是 |
| 工具结果、展示与 provider replay | `Session` 私有实现 | session | 是或受控外置 |
| prompt/user-context cache | 配对的 `ContextRuntime` | live session/run | 否 |
| runtime permission policy | `AppState.permissions` | runtime | 规则由配置层写入 |
| ToolCatalog、task tree、MCP/Skill runtime | 对应 AppState 状态胶囊 | runtime | 只持久化配置或对话产物 |
| turn、step、stream、tool progress | 调用栈 | 单次操作 | 否 |

`AppState` 是运行时状态图的唯一入口，不是进程全局 singleton。只有 application service、host 和 bootstrap 可以持有完整 AppState；Agent、Context、Tool 和 Provider adapter 只接收完成任务所需的最小对象或快照。

## Canonical conversation

`Session` 私有持有有序、不可变的 `ConversationEntry`：

- `HumanMessage`：真实用户输入，也是 turn 的唯一起点。
- `AssistantMessage`：一个成功完成的模型 step，可包含 text、reasoning 和 ToolCall。
- `ToolResultBatch`：闭合来源 AssistantMessage 的全部 ToolCall，不属于 human 或 assistant role。
- `ConversationSummaryMessage`：full compact 产生的可恢复摘要事实。
- `AttachmentMessage`：原位结构化辅助上下文，例如文件、Skill 正文或后台结果。

典型事实顺序为：

```text
HumanMessage
AssistantMessage(tool calls)
ToolResultBatch
AttachmentMessage[]
AssistantMessage(final text)
```

Assistant tool calls 与对应 batch 之间不能插入 Attachment。每个 `ToolResult` 保存模型可见内容、错误标志和安全 presentation；调用者不维护另一份展示索引。

Attachment 是否持久化由 Session 根据 payload 类型统一决定。显式文件、Skill 激活、invoked skills 和后台完成结果是 durable；Skill listing 和 Todo reminder 只存在于活动内存。非持久化 Attachment 不会成为 durable entry 的父节点，因此恢复后的因果链仍连续。

## Session 提交事务

`sessions.session.Session` 是唯一公开的 conversation 与持久化写入口。所有语义提交遵循同一顺序：

```text
从当前内存状态构造候选
  -> 校验父链、tool pairing、compact/replay 不变量
  -> 私有 store 原子写入完整变更
  -> 写入成功后替换内存状态
```

写入失败时 conversation、context entries、replay、工具结果索引和 compact boundary 都不推进。Agent、Chat、Context 和 Tool 不直接访问 JSONL codec、store、schema version、session 路径或工具结果目录。

进程运行期间，已打开 Session 的内存状态是读取权威；本地文件是跨进程恢复依据，不是 refresh API。

## 恢复与切换

`ChatService.resume_session()` 在 AppState operation lock 中：

1. 完整加载、校验并按需修复候选 Session。
2. 校验父链、tool pairing、compact boundary、presentation 和 replay 关联。
3. 从候选 facts 构造安全历史投影。
4. 仅在全部成功后调用 `AppState.replace_session()`。

替换 Session 会同时创建新的 `ContextRuntime`。任何一步失败都保留旧 Session 与 cache，不会出现旧 conversation 配新 context、工具结果或 replay 的混合状态。当前 provider、permission 和 workspace 不从 transcript 恢复。

恢复可以读取受支持的旧 record 形态；兼容逻辑只存在于 `sessions` 私有 codec。未知或冲突 schema 必须失败关闭，而不是静默丢弃事实。

## Compact 与模型工作集

`session.conversation` 是完整事实序列，`session.context_entries` 是从最近 compact boundary 开始的规划工作集。full compact 原子提交 summary、content replacements、boundary 和 replay 裁剪；完整 transcript 仍可用于历史展示和 feature 投影。

Provider replay 与 canonical assistant content 分离，通过 entry/content ID 和 provider binding 关联。切换 provider 保留可见事实，但不向 binding 不匹配的 adapter 发送旧 replay payload。

## 状态准入规则

- 新的长生命周期状态必须说明所有者、创建、更新、失效、销毁和持久化策略。
- request、turn、step、tool round、stream delta 和 pending approval 不得提升到 AppState 或 Session。
- 派生状态优先从 canonical facts 和不可变 snapshot 重算，不建立第二份可写权威来源。
- 跨状态胶囊的变更由明确 application use case 协调，不让 AppState 退化为任意服务查询容器。

主要源码入口：`src/my_code/runtime/state.py`、`src/my_code/conversation/models.py`、`src/my_code/sessions/session.py`。私有持久化实现在 `src/my_code/sessions/_*.py`，其他生产模块不得依赖它。
