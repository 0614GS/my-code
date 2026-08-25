# 工具系统

## 模块职责

`tools` 拥有工具定义、动态目录、不可变快照、输入校验、权限调用、执行和展示投影。内置文件与 Bash 工具位于 `tools.builtin`；TodoWrite 属于 `features.todos`，通过带来源的同一 Tool 接口注册。结果外置属于 Session 的私有持久化行为。

## Catalog 与 step 快照

`tools.catalog.ToolCatalog` 是 application 生命周期内的工具来源目录，由 `AppState.tools` 持有。每个来源使用稳定 `ToolSourceId` 整体注册、替换或撤销；重名会使更新原子失败，不会静默遮蔽已有工具。

Agent 在每个 step 开始时捕获带单调版本号的 `ToolCatalogSnapshot`。ContextPlanner 使用其中的 definitions 创建 ModelRequest，ToolRoundExecutor 使用同一快照解析并执行模型返回的调用。Catalog 在 stream 或执行期间发生变化时，当前 step 不漂移，下一 step 才看到新版本。

ToolExecutor 还把该 step 的只读工具映射和版本放入 `ToolContext`。Subagent Tool 据此创建 child-local catalog：显式 allowlist 必须是当前快照的子集；嵌套时重新绑定新的 Subagent Tool/parent task identity，而不是把父级可变 catalog 直接共享给 child。

## Tool 能力

具体 Tool 直接实现 `tools.base.Tool` 所需行为：

- 提供 provider-neutral `ModelToolDefinition`。
- 根据输入生成工具级权限判断。
- 在 `ToolContext` 中执行并返回 `ToolOutput`。
- 生成不含敏感原始数据的 presentation。

没有为单一实现建立额外 Tool port 或 adapter。

Subagent 属于 `features.subagents`，但仍实现同一 Tool 协议并经过上述输入、权限、取消和结果闭合管线。`subagents.enabled=false` 时 composition root 不注册该 source，AgentEngine 内没有 feature 条件分支。

`backgroundTasks.enabled=true` 时，同一 feature source 额外注册 TaskList、TaskOutput 和 TaskCancel。它们通过 ToolContext 的当前 run identity 做 owner 隔离；CLI/TUI 继续消费普通 Tool presentation 来显示 task ID/status，不直接访问 TaskSupervisor。

`skills.enabled=true` 时，`SkillRuntime` 在 application 启动阶段把当前 `SkillCatalogSnapshot` 适配为一个标准 `Skill` Tool。Tool description/schema 保持静态，可用列表由临时 `SkillListingAttachment` 提供。调用延迟读取 Markdown，并在完整 ToolResultBatch 后产生 durable `SkillActivationAttachment`。`allowed-tools` 转为 additive session allow rules，不收窄工具目录，且含权限请求的激活默认需要用户授权。旧 Tool adapter 持有旧 Skill snapshot，因此 reload 不会让当前 ToolRound 漂移。

## 执行管线

```text
ToolCall
  -> 当前 ToolCatalogSnapshot 查找与输入校验
  -> Tool 自身的权限分析
  -> PermissionPolicy 合并规则和 mode
  -> 必要时 PermissionPrompter 确认
  -> Tool 执行
  -> ToolResult(content + is_error + presentation)
  -> Session 提交完整 batch，并按需外置大结果
```

`ToolExecutor.execute()` 只返回带内嵌 presentation 的完整领域结果，不绑定活动 Session 或 result store。`present_result()` 每次执行只调用一次，展示异常使用稳定 fallback。Session 在提交 `ToolResultBatch` 时决定是否外置大结果，并原样保留 presentation。

权限检查规范化后的输入会再次校验。audit、prompter、hook、presentation 和 Tool 执行分别接收隔离的 JSON 副本；观察型扩展不能原地修改持久化 ToolCall 或真正执行的已批准输入。

## ToolRound

`tools.round_executor.ToolRoundExecutor` 按 AssistantMessage 中的顺序扫描 ToolCall。连续且 `Tool.is_concurrency_safe(input)` 为真的调用组成并行组；不安全或判断异常的调用单独执行，并作为前后屏障。Read、Glob、Grep 当前显式安全，未知/MCP/有副作用工具默认不安全。

并行组受 `tools.maxParallelCalls` 限制，默认值为 4，设为 1 可恢复串行。权限提示即使来自并行组也由 ToolExecutor 串行化。started/finished 事件携带 call ID，最终 ToolResultBatch 始终按原 ToolCall 顺序排列，不受完成先后影响。

取消时会回收当前组的全部 asyncio task，并为尚未执行完成的调用补齐稳定错误结果；已经完成的结果不会重复执行或丢失。单个调用失败不会隐式取消同组其他调用。

## 权限与工作区

- 输入含义和只读判断归具体 Tool。
- 全局 mode、allow/ask/deny 规则和确认顺序归 `permissions`。
- 路径解析、工作区逃逸防护和文件读写原语归 `workspace`。
- Bash AST、命令语义和重定向分析留在 `tools.builtin.bash`。

## 展示与持久化

模型可见内容与前端 presentation 是同一个不可变 `ToolResult` 的两个投影。Session v5 JSONL 将 presentation 内嵌到 `ToolResultRecord`；旧 sidecar 仅用于恢复兼容。大型原始输出和 microcompact 只替换 `content`，presentation 保持不变；provider adapter 明确忽略 presentation。
