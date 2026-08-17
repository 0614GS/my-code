# Agent 运行循环

## 1. 两层生命周期

运行时刻意分成两层：

| 层 | 生命周期 | 责任 | 入口 |
| --- | --- | --- | --- |
| `QueryEngine` | 整个会话 | 保存消息、用量、文件缓存、取消控制器；把内部事件转换为 SDK 输出 | `claude-code/src/QueryEngine.ts:184` |
| `query()` | 一次用户回合 | 在模型调用、工具执行、恢复逻辑之间循环，直到终止 | `claude-code/src/query.ts:219` |

`QueryEngine.submitMessage()` 先处理用户输入和本地命令，把用户消息写入 Transcript，再进入 `query()`。提前持久化很重要：即使进程在首个模型响应前退出，会话仍然可以恢复。

`query()` 是异步生成器。模型增量、内部消息、工具进度和边界事件会边产生边向上游发送，而不是等整个回合完成后返回一个大对象。

## 2. 一次循环迭代

主路径如下：

```text
读取 State
  → 构造本轮模型可见上下文
  → 检查/执行上下文压缩
  → 流式调用模型
  → 收集 assistant block 与 tool_use
  ├─ 没有 tool_use：运行 Stop hooks，结束或重试
  └─ 存在 tool_use：执行工具，加入 tool_result，进入下一次迭代
```

工具结果之后还会注入本轮新产生的附件，例如文件变化、队列消息、记忆、技能发现结果。注入完成后才建立下一轮 `State.messages`，避免普通用户消息插入 `tool_use` 与 `tool_result` 之间。

工具集合可以在两次迭代之间刷新，因此中途连上的 MCP server 不必等到下一个用户回合才生效。

## 3. 显式循环状态

`query.ts` 把跨迭代数据收敛到 `State`：

- `messages`：下一次迭代的上下文输入。
- `toolUseContext`：工具、权限、取消信号、文件缓存及状态访问器。
- `autoCompactTracking`：是否压缩过、距上次压缩多少轮、连续失败次数。
- `maxOutputTokensRecoveryCount`：输出截断恢复次数。
- `hasAttemptedReactiveCompact`：防止 prompt-too-long 恢复死循环。
- `pendingToolUseSummary`：与主模型调用并行生成的工具摘要。
- `stopHookActive`：标记当前是否处于 Stop hook 触发的重试链。
- `turnCount`：工具轮计数，用于 `maxTurns`。
- `transition`：上一次继续循环的原因。

继续和终止原因使用联合类型表示，见：

- `claude-code/src/query/transitions.ts`
- `claude-code/src/query/config.ts`

运行配置在进入 `query()` 时快照一次；循环状态则在每个 `continue` 点整体替换。这让“为什么又调了一次模型”可以从 transition 直接判断，而不必反推消息内容。

## 4. 流式工具执行

启用 `StreamingToolExecutor` 时，完整的 `tool_use` block 一到达就可以开始执行，不必等待模型流结束。执行器同时保证：

- 只读且声明为 concurrency-safe 的工具可以并行。
- 非 concurrency-safe 工具独占执行。
- progress 和已完成的 safe 工具结果可以及时发出；非 safe 工具构成不可跨越的执行屏障。
- 流式 fallback 时，旧执行器被 discard，新响应使用全新执行器。

非流式路径在采样结束后调用 `runTools()`，语义保持一致。调度细节见 [04-tool-system.md](04-tool-system.md)。

## 5. 恢复与终止

几个容易破坏对话协议的异常被放在循环内处理：

- **流式 fallback**：删除失败尝试产生的孤立 assistant 消息，清空工具结果，再以 fallback 模型重试。
- **模型调用抛错**：为已经出现的每个 `tool_use` 合成错误 `tool_result`，防止历史失配。
- **用户取消**：先让执行器为排队或运行中的工具补齐取消结果，再终止回合。
- **prompt too long**：先尝试已有的轻量 collapse，再尝试 reactive compact；每条恢复路径都有单次保护。
- **max output tokens**：可先提高单次输出上限，再注入继续提示；恢复次数有上限。
- **API 错误**：不运行 Stop hooks，避免“错误 → hook 阻塞 → 重试同一错误”的循环。
- **maxTurns / budget**：在进入下一工具轮前检查，达到限制后返回结构化结果。

`turnCount` 和 `maxTurns` 是 Claude Code 参考源码的原始符号。在 nano-code
术语中，这类单次用户输入内的循环单元称为 Step，避免与 Turn 混淆。

可恢复错误在恢复结果确定前不会先暴露给 SDK 调用方，否则调用方可能看到中间错误后提前关闭连接。

## 6. 核心不变量

1. 每个已发出的 `tool_use` 最终都必须有同 ID 的 `tool_result`，包括取消和异常路径。
2. 只有完成本轮工具结果与附件组装后，才建立下一次迭代的消息数组。
3. fallback 不能复用失败尝试的 tool ID、thinking block 或执行器状态。
4. API 没有产生有效回答时，不执行依赖回答内容的 Stop hooks。
5. 循环退出原因是显式数据，不使用“最后一条消息长什么样”作为唯一状态机。

## 7. 主要源码入口

- `claude-code/src/QueryEngine.ts`
- `claude-code/src/query.ts`
- `claude-code/src/query/{config,deps,transitions,stopHooks}.ts`
- `claude-code/src/services/api/claude.ts`
- `claude-code/src/services/tools/StreamingToolExecutor.ts`
- `claude-code/src/services/tools/toolOrchestration.ts`

## 8. nano-code 的用户回合边界

当前 Python 实现把 `AgentEngine` 收敛为用户回合状态机：写入用户消息、请求
`ContextPort`、通过 `ModelCallPort` 消费统一模型事件、判断工具轮是否继续、处理
overflow/取消/max-steps，并发出 `AgentEvent`。主 Agent 默认不限制 Step 数；只有
settings 或入口显式提供 `max_steps` 时才在完整提交当前 ToolRound 后停止，并返回
独立的 `AgentMaxStepsReached` 终态，而不是抛出通用异常。失败或响应式压缩重试的
ModelCall 不增加 completed step 计数。会话事实由 `ConversationState` 管理，
工具轮由 `ToolRoundPort` 管理，摘要由 `Compactor` 管理。

六边形边界由 `nano_code.agent` 统一声明：CLI/TUI 通过 `AgentInboundPort` 调用
`submit`、`stream`、`status`、`context_status`、compact 和 resume；Engine 通过下列 outbound port
请求外部能力。`context`、`providers`、`sessions` 和 `tools` 只实现这些协议或重新
导出它们，不拥有 Agent-facing port 的第二份声明。

| Port | 方向 | 适配器职责 |
| --- | --- | --- |
| `AgentInboundPort` | inbound | 驱动用户回合、状态查询、compact 与 session resume |
| `ContextPort` | outbound | 从 `ConversationSnapshot` 生成请求、预算和 compact 视图 |
| `ModelCallPort` / `ModelCompletionPort` | outbound | 流式主模型调用与完整摘要模型请求 |
| `ToolRoundPort` | outbound | 串行工具轮、取消闭合和展示 DTO |
| `SessionRepository` | outbound | 初始化/resume 时加载 Transcript，运行期只追加写入 |
| `Compactor` | outbound | 返回尚未持久化的摘要提交计划 |

`ProviderRouter` 在 provider 边界把完整响应 fallback 适配为最终事件，所以 Engine
不再判断某个具体 provider 是否支持流式。`ModelCompletionPort.complete()` 仍保留给
`CompactionService` 这种独立摘要请求使用。`AgentStatus` 是一次只读查询结果，不是
可变状态容器；port 不再同时暴露 session ID、消息数组和计数 property。CLI/TUI 只
消费 runtime DTO，不读取 ConversationMessage、SessionStore、ToolExecutor 或
ContextPlanner 的内部字段。

TodoList 同时通过 `AgentStatus.todos` 快照和 `AgentTodoListUpdated` 流事件暴露。事件
只会在包含成功 TodoWrite 的工具结果消息完成持久化并进入 `ConversationState` 后产生，
所以前端不会看到尚未提交或最终失败的状态。

`ConversationState` 在构造或显式 resume 时从 `SessionRepository.load()` hydration
一次。此后每次模型请求都从内存会话状态投影；Transcript 是崩溃恢复日志，
不是运行期的同步读模型。写入仍是同步且持久化优先，成功后状态才前进。
