# Agent 运行循环

## 所有权

`agent.engine.AgentEngine` 是无活动会话状态的 turn 执行器。它持有模型调用能力、`ContextEngine`、工具轮执行器和 step 配置；活动 `Session` 与用户输入由 application service 逐次传入。

`chat.service.ChatService` 是用户级编排器，只持有一个 `AppState` 入口，并通过同一 operation lock 串行化 submit、stream、compact、resume 和 provider 切换。活动 Session、权限和 ProviderRuntime 的权威引用都在 AppState。

第一次 submit/stream 在 operation lock 内依次启动 `AppState.mcp` 与 `AppState.skills`。成功发现的远端工具和 Skill selector 只更新 application-lifetime ToolCatalog，因此第一个模型 step 会捕获完成启动后的快照；连接管理与目录扫描不进入 Agent loop。

前台 Agent 继续使用 ProviderRouter；并发 child run 由 `AgentRunFactory` 创建，每个 run 持有独立 Session、Agent 组件和 `ProviderClientLease`。已有 run 捕获创建时的 provider binding，运行期 provider switch 只影响之后创建的 lease。

foreground Subagent 由模型可见的标准 Tool 启动。`SubagentController` 只把显式 prompt/attachments 交给 child，不复制父 transcript；child 结束后关闭 run/lease，并把一个结构化终态作为父 ToolResult 返回。调用方取消 foreground 等待时，TaskSupervisor 会取消 child task，child run 的 `finally` 负责关闭 lease。

启用 background gate 后，Subagent 与 Bash 都可立即返回 task ID；普通 Bash 超过其 timeout 前台等待预算后会把同一进程原地转为后台，不再把 timeout 当作运行期限。TaskList/TaskCancel 通过 root run owner 访问两类任务，完成通知由 attachment source 在后续 step 规划前产生。Session 接受 durable payload 后才 acknowledge registry；Conversation 按 task ID 去重，所以 inspect、失败规划、compact retry 或 resume 不会吞掉或重复通知。

## 一次 Turn

```text
ChatService.stream(prompt)
  -> 加载显式 attachment
  -> AgentEngine.stream(active_session, input)
     -> 先提交 HumanMessage
     -> Step 1: 捕获一次 ToolCatalogSnapshot
        -> 运行动态 attachment sources，并由 Session 排入 Conversation
     -> ContextEngine.plan(Session snapshot, 同版本 tool definitions)
     -> ModelClient.stream()
     -> 提交完整 AssistantMessage
     -> 无工具：产生 AgentTurnSucceeded
     -> 有工具：ToolRoundExecutor 使用同一 tool snapshot
        -> Session 原子提交 ToolResultBatch
        -> 按 ToolCall 顺序提交成功调用的 Attachment
        -> 最后应用 session permission updates
     -> Step 2..N: 重新投影 Context 并调用模型
     -> 达到上限：产生 AgentMaxStepsReached
  -> 投影为 Chat events
```

模型返回的文本、reasoning 和工具调用只有在完整响应形成后才成为 Conversation facts。展示增量可以先发送给 host，但不会成为可恢复消息。

## 终止与失败

- 正常完成与 step 上限直接使用 `agent.models` 中的两个终态值，不再额外包装 completed event。
- provider 缺少最终响应、事件序号不连续或展示块重叠时立即失败。
- context overflow 先尝试一次 reactive compact，再重新构造请求。
- 工具取消时为尚未完成的调用补齐错误结果，提交闭合的 ToolResultBatch 后继续抛出取消。
- 网络或模型失败不会撤销已经提交的 HumanMessage，恢复时仍能看到用户输入。
- 带 token ceiling 的 child run 会累计每个完整响应的 input/cache/output usage；已完成响应不会被丢弃，达到上限后下一次模型请求以结构化 task failure 终止。

## 不变量

- Agent 不拥有活动 Session，也不负责 session catalog 或 resume。
- Agent 不提供 context inspection、手动 compact 或历史 tool presentation API。
- 一个模型请求只接受一个最终 `ModelOutputCompleted`。
- AssistantMessage 必须先持久化，工具才可以执行。
- 同一 step 的 ModelRequest 和 ToolRound 必须使用同一 ToolCatalogSnapshot。
- Catalog 更新只影响下一个 step，不改变正在运行的 ToolRound。
- Skill activation 在 ToolResultBatch 后成为 durable Attachment；正文不修改 system prompt，并在 compact/resume 后继续存在。
- 每个 ToolCall 最终都有一个 ToolResult。
- 一个 AppState 同一时刻只运行一个会改变状态的 application operation。
- runtime 关闭先取消 TaskSupervisor 中的任务，再回收 AgentRun、Skill runtime、MCP transport，最后关闭 provider clients。

主要入口：`agent.engine`、`agent.events`、`agent.models`、`chat.service`。
