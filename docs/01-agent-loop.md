# Agent 运行循环

## 所有权

`agent.engine.AgentEngine` 是无活动会话状态的 turn 执行器。它持有模型调用能力、`ContextEngine`、工具轮执行器和 step 配置；活动 `Session` 与用户输入由 application service 逐次传入。

`chat.service.ChatService` 是用户级编排器，只持有一个 `AppState` 入口，并通过同一 operation lock 串行化 submit、stream、compact、resume 和 provider 切换。活动 Session、权限和 ProviderRuntime 的权威引用都在 AppState。

## 一次 Turn

```text
ChatService.stream(prompt)
  -> 加载显式 attachment
  -> AgentEngine.stream(active_session, input)
     -> 先提交 HumanMessage
     -> Step 1: ContextEngine.plan(Session snapshot)
     -> ModelClient.stream()
     -> 提交完整 AssistantMessage
     -> 无工具：产生 AgentTurnSucceeded
     -> 有工具：ToolRoundExecutor -> Session 提交 ToolResultBatch
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

## 不变量

- Agent 不拥有活动 Session，也不负责 session catalog 或 resume。
- Agent 不提供 context inspection、手动 compact 或历史 tool presentation API。
- 一个模型请求只接受一个最终 `ModelOutputCompleted`。
- AssistantMessage 必须先持久化，工具才可以执行。
- 每个 ToolCall 最终都有一个 ToolResult。
- 一个 AppState 同一时刻只运行一个会改变状态的 application operation。

主要入口：`agent.engine`、`agent.events`、`agent.models`、`chat.service`。
