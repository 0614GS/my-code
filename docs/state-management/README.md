# 状态管理重构（已实现）

本目录记录已经落地的状态管理目标模型、迁移顺序和验收标准。顶层 `docs/` 已同步为当前实现；本目录保留设计理由、兼容决策和逐项验收证据。

本轮重构解决了三个问题：

1. runtime 状态统一从 `AppState` 进入，并拆分为 Session、PermissionState、ProviderRuntime 和 WorkspaceState；
2. `Session` 成为内存对话事实和本地持久化的唯一公开边界；
3. `ModelRequest.input` 改为 provider-neutral items，工具结果不再建模为 Human/User message。

目标不是创建一个所有模块都能任意访问的全局变量，而是建立一个明确的 runtime 状态所有者，并让其内部状态胶囊各自保持唯一权威来源。

## 文档索引

| 文档 | 内容 |
| --- | --- |
| [00-baseline-decisions.md](00-baseline-decisions.md) | 阶段 0 行为证据与冻结决策 |
| [01-state-ownership.md](01-state-ownership.md) | `AppState`、`Session`、权限、Provider 与短生命周期状态的所有权 |
| [02-session-and-model-input.md](02-session-and-model-input.md) | Session 事实、turn/step 术语、Context 投影和跨 Provider 输入模型 |
| [03-migration-plan.md](03-migration-plan.md) | 分阶段迁移 TODO、依赖顺序和每阶段退出条件 |
| [04-acceptance-criteria.md](04-acceptance-criteria.md) | 行为、失败、恢复、Provider 映射和架构验收标准 |

## 统一术语

- **Turn**：从一次真实用户输入开始，到下一次真实用户输入之前结束。一个 turn 可以包含多个 step。
- **Step**：一次模型调用。模型请求失败仍是一次运行期 step 尝试；只有得到完整模型响应后，才会形成一条可持久化的 `AssistantMessage`。
- **Tool round**：一个 `AssistantMessage` 中全部工具调用的执行与结果闭合。完成后可以触发同一 turn 的下一个 step。
- **Session entry**：Session 内按顺序保存的 provider-neutral 事实或 session 级输入条目，不等同于 Provider API 的 message。
- **Model input item**：ContextEngine 为单次模型调用生成的 provider-neutral 输入项。
- **Wire payload**：OpenAI Responses 或 Anthropic Messages adapter 最终生成的 SDK/API 参数。

一个典型 turn 的事实顺序是：

```text
HumanMessage
  -> Step 1 -> AssistantMessage(tool calls)
  -> ToolResultBatch
  -> Step 2 -> AssistantMessage(text)
```

不得把 `AssistantMessage` 命名为 assistant turn，也不得用 Provider 的 user role 定义 `ToolResultBatch` 的领域归属。

## 核心决策

- `AppState` 是活动 runtime 状态的唯一入口，但不是可以从任意模块访问的 service locator。
- `Session` 是对话事实、session 级临时输入和对话相关持久化的唯一所有者。
- `Conversation` 若在迁移期保留，只能是 `Session` 的私有聚合实现，不能继续作为平级状态入口。
- ContextEngine 无状态地消费不可变 Session snapshot，不保存第二份 history，也不直接提交 Session。
- `ModelRequest.input` 使用语义 item，而不是假设所有输入都是 user/assistant message。
- Tool result 与 Human input 是并列语义；只有 Anthropic adapter 会把 tool result 包装进 user-role message。
- OpenAI/Anthropic 的 continuation 数据作为不透明 replay sidecar 保存，不进入 canonical message 字段。
- 流式 delta、step 计数、tool 执行进度和 pending approval 都是短生命周期状态。

## 使用规则

- 后续改变这些边界的 PR 应引用 [04-acceptance-criteria.md](04-acceptance-criteria.md) 中受影响的验收 ID。
- 变更必须保持 CLI 可运行，且不能引入第二个可写 conversation 或 runtime permission 权威来源。
- 若推翻本目录的核心决策，应先更新文档并记录理由，再修改生产代码。
