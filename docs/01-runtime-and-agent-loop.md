# Runtime 与 Agent loop

本文沿一次用户输入说明运行主路径。组件关系见[总体架构](00-architecture.md)，对话提交细节见[状态、对话与 Session](02-state-conversation-sessions.md)。

## 所有权

`application.service.ApplicationService` 协调用户级用例，只持有一个 `runtime.state.AppState`。`AppState` 的 operation lock 串行化 submit、stream、compact、resume、模型切换和其他会改变活动状态的操作。

`agent.engine.AgentEngine` 是不持有活动会话的 turn 执行器。Session、`ContextRuntime`、工具快照和输入由调用方显式传入；session catalog、resume、历史展示和 provider 配置不属于 Agent。

首次交互前，AppState 并行启动可选的 MCP 与 Skill runtime。成功发现的能力只更新 application-lifetime `ToolCatalog`，不会把连接或扫描状态放进 Agent loop。

## 一次 Invocation 与 step-boundary steering

```text
ApplicationService.stream_interactive()
  -> 获取 AppState operation lock
  -> PendingInputController 并行准备 attachment
  -> AgentEngine.stream_continuation(..., pending_source)
     -> 首次请求前 drain 全部 ready input
     -> Session.commit_user_inputs() 原子提交相邻 HumanMessage + durable attachment
     -> Step 1 捕获 ToolCatalogSnapshot / ToolExposureSnapshot 与 PermissionPolicy
     -> 派生动态 Attachment 并由 Session 接受
     -> ContextEngine.plan(...) 生成 ModelRequest
     -> Session.prepare_model_invocation() 原子提交 request audit blobs + manifest
     -> ModelClient.stream()
     -> Session.finish_model_invocation() 记录 completed/failed/cancelled/overflow
     -> 完整响应提交为 AssistantMessage
     -> 无工具：形成完整 step boundary；有 pending input 时提交并继续下一 step
     -> 有工具：使用同一 step 工具与权限快照执行 ToolRound
        -> Session 原子提交 ToolResultBatch
        -> 提交成功调用产生的 Attachment
        -> 应用已验证的 session permission updates
     -> ToolResultBatch 与工具 attachment 原子闭合后形成 step boundary
     -> 未耗尽 max_steps 时 drain pending input，再从最新 Session 状态规划
  -> ApplicationEvent 投影给 host
```

模型的 text、reasoning 和 tool call 只有形成完整最终响应后才成为对话事实。流式 delta 可以先展示，但不会写入 Session。一次 invocation 可在多个完整 step 边界接收多条独立用户 steering message；这些消息保持 FIFO，不会插入 ToolCall 与 ToolResultBatch 之间。`max_steps` 限制单次 invocation 的模型调用次数；预算耗尽后未接受的输入由 host 在新 invocation 中继续处理。

Headless `submit/stream` 不提供 pending source，保留单输入行为。前台与后台 continuation 共用活动 Session 的 pending source，因此用户输入会优先进入任一当前运行 invocation 的下一个安全边界。

完整 compact 具有显式 lifecycle。Agent 的 auto/reactive 路径在摘要请求前发出 started，在 `Session.commit_compaction()` 成功后发出 completed；Application 的 `/compact` 路径通过 `stream_compaction()` 提供相同的 frontend-neutral 事件。失败和取消沿原调用异常传播，不伪造 completed。轻量 microcompact 不属于这一 lifecycle。

所有 session-bound 模型调用都遵守 audit-before-delivery：主 Agent、后台 continuation、child run 及 manual/auto/reactive compact 在 Provider 收到请求前，先持久化 provider-neutral 的 `ModelInvocation`。初始审计写入失败会阻止网络请求；Provider 终态随后追加到 sidecar。进程在 prepared 后中断时，恢复投影为 `delivery-unknown`，不会猜测 Provider 是否已接收。

## 工具与扩展运行

每个 step 只捕获一次工具目录、曝光视图和权限策略。请求中的 definitions、ToolCall 校验和后续 ToolRound 使用同一工具快照；运行中切换 permission mode 会立即持久化并更新 UI，但当前 step 继续使用已捕获的 mode/rules，下一 step 才重新捕获。MCP refresh、Skill reload 或其他目录更新也只影响下一个 step。

Foreground Subagent 由标准 Tool 启动。`SubagentController` 只传递显式 prompt 和 attachments，不复制父 transcript；child 使用独立 Session、`ContextRuntime`、Agent 组件和 provider lease，最终只向父 ToolCall 返回一个结构化结果。

启用后台任务后，Bash 或 Subagent 可以先返回 task ID。`TaskSupervisor` 管理终态和取消树，完成通知在安全的后续 step 作为 durable attachment 交付；进度事件和运行中状态不写 Conversation。

## 失败与取消

- Provider、事件序列或 context 规划失败不会提交部分 `AssistantMessage`；已经提交的 `HumanMessage` 保留。
- request audit 初始提交失败时 Provider 不会被调用；compact 的 summary/boundary 也不会提交。
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
- 用户 steering 只能在首次 plan 前、无工具响应提交后或完整 ToolRound 提交后进入 canonical Conversation。
- 每个 ToolCall 最终都有一个 ToolResult，完成顺序不改变结果顺序。
- 同一 AppState 同时只运行一个会改变活动状态的 application operation。
- runtime 关闭先停止任务和 child run，再关闭扩展 transport 与 provider client。

主要源码入口：`src/my_code/application/service.py`、`src/my_code/application/turns/coordinator.py`、`src/my_code/runtime/state.py`、`src/my_code/agent/engine.py`、`src/my_code/tools/round_executor.py`。
