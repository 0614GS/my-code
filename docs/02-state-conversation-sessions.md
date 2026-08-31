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
| runtime permission policy | `AppState.permissions` | runtime | mode 由 Session 写入，规则由配置或 durable facts 恢复 |
| ToolCatalog、task tree、MCP/Skill runtime | 对应 AppState 状态胶囊 | runtime | 只持久化配置或对话产物 |
| turn 身份与终态 journal | `Session` | session | 是（不复制正文） |
| provider-neutral request audit | `Session` 私有 sidecar | session | 是（不进入 Conversation） |
| step、stream、tool progress | 调用栈 | 单次操作 | 否 |
| pending user input、附件准备状态 | host/runtime 内存 | 当前进程与活动 Session | 否 |

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

文件写入结果可以在同一 presentation 中嵌入前端中立的结构化 diff，包括路径、
created/updated、完整增删统计、hunk、旧新行号、文件末尾换行状态和省略计数。它随
`ToolResult` 一同写入现有 v5 record；codec 兼容旧的三字段 presentation，并对带 diff
的新嵌套结构逐层执行精确字段和类型校验。恢复只重放该持久化快照，不重新读取文件。

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

## Request audit sidecar

每个 Session 可拥有独立的 `<session-id>/request-audit.jsonl`。它不是 canonical Conversation，也不参与 context planning。私有 store 对规范化 JSON 使用 SHA-256 内容寻址，system prompt section、有序 model input item 和 tool definition/schema 只写一次；每个 request manifest 按顺序引用 blobs，并保存 purpose、causal head、step/attempt、origin、输出预算、reasoning mode 与 ContextBudget 摘要。终态用独立 record 追加。

sidecar 不保存 API key、认证头、Provider wire role 归一化或 opaque continuation。允许披露的 reasoning 只按 provider-neutral presentation 记录；hidden/redacted 内容不会因 audit 而出现。

旧 Session 没有 sidecar 仍可恢复，Transcript 明确显示历史审计缺口。sidecar 一旦存在，哈希不符、悬空引用、重复终态或 manifest 序号损坏都会使 Session 恢复失败关闭；不会用当前 prompt/tool schema 伪造历史请求。

每个 Agent turn 在处理前提交 `turn_started`，并在发布终态事件前提交
`turn_finished`。`Session.turn_history` 按开始顺序返回只读聚合；未闭合 start 保持可见，
供恢复和 Harness 判断 incomplete。Journal 写入故障不会覆盖 Agent 本身的执行语义。

## 恢复与切换

`ChatService.resume_session()` 完整验证候选后，在 AppState operation lock 中原子发布；存在未接受 pending input 时拒绝切换，避免把临时输入错误绑定到另一 Session：

1. 完整加载、校验并按需修复候选 Session。
2. 校验父链、tool pairing、compact boundary、presentation 和 replay 关联。
3. 从候选 facts 构造安全历史投影。
4. 恢复候选 Session 最后持久化的 permission mode。
5. 仅在全部成功后调用 `AppState.replace_session()`。

替换 Session 会同时创建新的 `ContextRuntime` 并发布恢复后的 permission mode。任何一步失败都保留旧 Session、mode 与 cache，不会出现旧 conversation 配新 context、工具结果或 replay 的混合状态。当前 provider、permission rules 和 workspace 不从 transcript 的 mode 记录恢复。

Permission mode 使用 last-wins 的 session record；旧 transcript 没有该记录时回退到 `session_started.permission_mode`。运行中切换遵循 persistence-first，写入失败时 runtime policy 不改变。显式 CLI override 优先于保存值并成为该 Session 的新值。

Transcript 当前 schema 为 v6；v5 及更早 session 明确拒绝恢复。兼容逻辑只存在于 `sessions` 私有 codec，未知或冲突 schema 必须失败关闭，而不是静默丢弃事实。assistant fact 保存 Provider usage 与响应后的 context footprint；compact boundary 保存 `pre_compact_tokens` 及其 `reported|estimated` 来源。

## Compact 与模型工作集

`session.conversation` 是完整事实序列，`session.context_entries` 是从最近 compact boundary 开始的规划工作集。full compact 原子提交 summary、content replacements、boundary 和 replay 裁剪；完整 transcript 仍可用于历史展示和 feature 投影。

Provider replay 与 canonical assistant content 分离，通过 entry/content ID 和 provider binding 关联。切换 provider 保留可见事实，但不向 binding 不匹配的 adapter 发送旧 replay payload。

## 状态准入规则

- 新的长生命周期状态必须说明所有者、创建、更新、失效、销毁和持久化策略。
- request、turn、step、tool round、stream delta 和 pending approval 不得提升到 AppState 或 Session。
- request manifest 是 Session 持久化的审计事实，但不是 `ConversationEntry`，不得被 Context 当作模型历史重放。
- pending input queue 不是 canonical Session；只有 `commit_user_inputs()` 成功后，独立的 HumanMessage 与 durable attachment 才进入 JSONL 和内存 Conversation。
- 同一边界的多条用户输入先在候选 aggregate 中完整验证，再通过一次 message batch append 持久化；成功后才发布内存状态和 accepted 事件。
- 派生状态优先从 canonical facts 和不可变 snapshot 重算，不建立第二份可写权威来源。
- 跨状态胶囊的变更由明确 application use case 协调，不让 AppState 退化为任意服务查询容器。

主要源码入口：`src/my_code/runtime/state.py`、`src/my_code/conversation/models.py`、`src/my_code/sessions/session.py`。私有持久化实现在 `src/my_code/sessions/_*.py`，其他生产模块不得依赖它。
