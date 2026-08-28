# Runtime 与 Agent loop

本文沿一次用户输入说明运行主路径。组件关系见[总体架构](00-architecture.md)，对话提交细节见[状态、对话与 Session](02-state-conversation-sessions.md)。

## 所有权

`chat.service.ChatService` 协调用户级用例，只持有一个 `runtime.state.AppState`。`AppState` 的 operation lock 串行化 submit、stream、compact、resume、模型切换和其他会改变活动状态的操作。

`agent.engine.AgentEngine` 是不持有活动会话的 turn 执行器。Session、`ContextRuntime`、工具快照和输入由调用方显式传入；session catalog、resume、历史展示和 provider 配置不属于 Agent。

首次交互前，AppState 并行启动可选的 MCP 与 Skill runtime。成功发现的能力只更新 application-lifetime `ToolCatalog`，不会把连接或扫描状态放进 Agent loop。

## 一次 Turn

```text
ChatService.stream(prompt)
  -> 获取 AppState operation lock
  -> 加载显式 attachment
  -> AgentEngine.stream(Session, ContextRuntime, input)
     -> Session 提交 HumanMessage
     -> Step 1 捕获 ToolCatalogSnapshot / ToolExposureSnapshot
     -> 派生动态 Attachment 并由 Session 接受
     -> ContextEngine.plan(...) 生成 ModelRequest
     -> ModelClient.stream()
     -> 完整响应提交为 AssistantMessage
     -> 无工具：结束 turn
     -> 有工具：使用同一 step 工具快照执行 ToolRound
        -> Session 原子提交 ToolResultBatch
        -> 提交成功调用产生的 Attachment
        -> 应用已验证的 session permission updates
     -> Step 2..N 重新从最新 Session 状态规划
  -> ChatEvent 投影给 host
```

模型的 text、reasoning 和 tool call 只有形成完整最终响应后才成为对话事实。流式 delta 可以先展示，但不会写入 Session。一个 turn 可以包含多个 step；`max_steps` 限制本 turn 的模型调用次数。

## 工具与扩展运行

每个 step 只捕获一次工具目录和曝光视图。请求中的 definitions、ToolCall 校验和后续 ToolRound 使用同一快照；MCP refresh、Skill reload 或其他目录更新只影响下一个 step。

Foreground Subagent 由标准 Tool 启动。`SubagentController` 只传递显式 prompt 和 attachments，不复制父 transcript；child 使用独立 Session、`ContextRuntime`、Agent 组件和 provider lease，最终只向父 ToolCall 返回一个结构化结果。

启用后台任务后，Bash 或 Subagent 可以先返回 task ID。`TaskSupervisor` 管理终态和取消树，完成通知在安全的后续 step 作为 durable attachment 交付；进度事件和运行中状态不写 Conversation。

## 失败与取消

- Provider、事件序列或 context 规划失败不会提交部分 `AssistantMessage`；已经提交的 `HumanMessage` 保留。
- context overflow 允许执行一次 reactive compact，再从最新 Session 状态重新规划。
- 工具轮取消时，执行器取消当前并行组，为未完成调用生成稳定错误结果，按原 ToolCall 顺序提交闭合 batch，然后继续传播取消。
- Session 恢复仍会幂等修复 transcript 尾部遗留的未闭合 ToolCall，但这不是正常取消路径的替代品。
- 带 token ceiling 的 child run 保留已经完成的 Provider 响应，只在下一次请求前阻止继续消费。
- foreground 等待被取消时，TaskSupervisor 取消 child task；run 的关闭路径负责释放 lease。

## 启动与关闭

```text
bootstrap 组装 -> AppState.start() -> MCP / Skills -> 接受 turn

AppState.close():
TaskSupervisor -> AgentRunFactory -> SkillRuntime -> McpRuntime -> ProviderRuntime
```

关闭会继续尝试回收后续资源，并汇总各阶段异常。运行期 Provider 切换只影响前台 router 和之后创建的 lease；已运行的 child 保持创建时捕获的 binding。

## 必须保持的不变量

- Agent 不拥有活动 Session、AppState 或第二份 conversation。
- `AssistantMessage` 必须在工具执行前提交。
- 同一 step 的请求和 ToolRound 使用同一 `ToolExposureSnapshot`。
- 每个 ToolCall 最终都有一个 ToolResult，完成顺序不改变结果顺序。
- 同一 AppState 同时只运行一个会改变活动状态的 application operation。
- runtime 关闭先停止任务和 child run，再关闭扩展 transport 与 provider client。

主要源码入口：`src/my_code/chat/service.py`、`src/my_code/runtime/state.py`、`src/my_code/agent/engine.py`、`src/my_code/tools/round_executor.py`。
