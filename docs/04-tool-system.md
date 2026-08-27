# 工具系统

## 模块职责

`tools` 拥有工具定义、动态目录、不可变快照、输入校验、权限调用、执行和展示投影。内置文件与 Bash 工具位于 `tools.builtin`；TodoWrite 属于 `features.todos`，通过带来源的同一 Tool 接口注册。结果外置属于 Session 的私有持久化行为。

## Catalog、曝光与 step 快照

`tools.catalog.ToolCatalog` 是 application 生命周期内的工具来源目录，由 `AppState.tools` 持有。每个来源使用稳定 `ToolSourceId` 整体注册、替换或撤销；重名会使更新原子失败，不会静默遮蔽已有工具。

`ToolCatalogSnapshot` 始终包含完整、可执行的工具全集。Agent 在每个 step 开始时再基于 Session discovery state 生成一次不可变 `ToolExposureSnapshot`；ContextPlanner 只发送其中实际曝光的 definitions，ToolRoundExecutor 使用同一快照校验、分组和执行。Catalog 在 stream 或执行期间发生变化时，当前 step 不漂移，下一 step 才看到新版本。

Tool 默认声明为 `eager`；TodoWrite、TaskList、TaskCancel 和全部 MCP adapter 声明为 `searchable`。`tools.toolSearchMode` 默认为 `dispatcher`：ToolSearch 与 InvokeSearchedTool 常驻，搜索结果的完整 schema 通过 durable attachment 提供，顶层 definitions 不随搜索变化。`native` 模式从搜索后的下一 step 起把命中的完整 definition 追加到已排序 eager 前缀之后。两种模式都不使用 provider 专用的 `tool_reference` 或 `defer_loading` 字段。

ToolExecutor 还把该 step 的只读工具映射和版本放入 `ToolContext`。Subagent Tool 据此按固定角色创建 child-local catalog，而不是接受模型提供的 allowlist：`explore` 只取父快照中的 Read、Glob、Grep、Bash，并给每个调用增加不可绕过的只读代理；`general` 继承完整父快照。嵌套时 Subagent 与 Task tools 重新绑定 child identity，最大深度时移除 Subagent。

## Tool 能力

具体 Tool 直接实现 `tools.base.Tool` 所需行为：

- 提供 provider-neutral `ModelToolDefinition`。
- 根据输入生成工具级权限判断。
- 在 `ToolContext` 中执行并返回 `ToolOutput`。
- 生成不含敏感原始数据的 presentation。

没有为单一实现建立额外 Tool port 或 adapter。

Subagent 属于 `features.subagents`，但仍实现同一 Tool 协议并经过上述输入、权限、取消和结果闭合管线。`subagents.enabled=false` 时 composition root 不注册该 source，AgentEngine 内没有 feature 条件分支。

启动 Subagent 本身是只读编排操作，default mode 下直接放行，前台和后台都不弹确认框；显式的整工具 `Subagent` ask/deny 仍由统一策略执行。child 复制父级 mode/rules，其 Read、Bash、写入和 MCP 等真实调用继续逐次经过标准权限管线。

Subagent 只提供 `explore` 与 `general` 两个 built-in definition。两者使用独立 Session、run、provider lease、ContextRuntime 和专用 prompt registry，只接收显式 prompt/attachments；不复制父 transcript。Explore 的只读代理在权限判断前及执行前各检查一次底层 `is_read_only()`，因此 Bash 重定向或修改命令在 allow rule 和 bypass 模式下仍会被拒绝。

TUI host 中 `backgroundTasks.enabled=true` 时，主 Session 的 Bash schema 增加 `background`，并注册 TaskList 与 TaskCancel。二者统一覆盖 Bash 和 Subagent，通过 ToolContext 的当前 run identity 做 owner 隔离；输出不经过 TaskOutput，而由 completion attachment 给出受控临时文件绝对路径，再用 Read 获取。TUI 继续消费普通 Tool presentation，不直接访问 TaskSupervisor。Subagent 的 Bash 快照不暴露 `background`，执行层会拒绝注入该字段。

`skills.enabled=true` 时，`SkillRuntime` 在 application 启动阶段把当前 `SkillCatalogSnapshot` 适配为一个标准 `Skill` Tool。Tool description/schema 保持静态，可用列表由临时 `SkillListingAttachment` 提供。调用延迟读取 Markdown，并在完整 ToolResultBatch 后产生 durable `SkillActivationAttachment`。`allowed-tools` 转为 additive session allow rules，不收窄工具目录，且含权限请求的激活默认需要用户授权。旧 Tool adapter 持有旧 Skill snapshot，因此 reload 不会让当前 ToolRound 漂移。

## 执行管线

```text
ToolCall
  -> 当前 ToolExposureSnapshot 直接曝光/dispatch 校验
  -> 同 step ToolCatalogSnapshot 查找与目标输入校验
  -> Tool 自身的权限分析
  -> PermissionPolicy 合并规则和 mode
  -> 必要时 PermissionPrompter 确认
  -> Tool 执行
  -> ToolResult(content + is_error + presentation)
  -> Session 提交完整 batch，并按需外置大结果
```

`ToolExecutor.execute()` 只返回带内嵌 presentation 的完整领域结果，不绑定活动 Session 或 result store。`present_result()` 每次执行只调用一次，展示异常使用稳定 fallback。Session 在提交 `ToolResultBatch` 时决定是否外置大结果，并原样保留 presentation。

Dispatcher 只解析外层 `{tool_name, arguments}` 并验证目标仍是已搜索且 fingerprint 有效的 searchable tool；真实目标随后完整经过自己的 validation、权限策略、用户确认、audit、hook、取消和 I/O。ToolResult 保留外层 provider tool-use ID，而展示、权限和审计使用真实目标 identity。同轮 ToolSearch 结果在整个 ToolRound 成功提交后才生效，因此同轮 Invoke 不能提前消费。

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
- Bash 分析同时生成确定性的 remembered-allow 建议：稳定 dispatcher 使用两段
  前缀（例如 `git push:*`、`uv run:*`），包装器、复合或动态命令使用转义后的
  精确命令；冗余的当前工作区 `cd ... &&` 不进入建议。
- Explore 的只读代理消费工具提供的结构化只读判定及原因；未知 Bash
  语法继续 fail-closed，精确当前 cwd 的冗余 `cd ... &&` 只在权限证明中视为
  空操作，deny/ask 规则仍检查原始命令。

## 展示与持久化

模型可见内容与前端 presentation 是同一个不可变 `ToolResult` 的两个投影。Session v5 JSONL 将 presentation 内嵌到 `ToolResultRecord`；旧 sidecar 仅用于恢复兼容。大型原始输出写入 session 临时目录并在 transcript 中保留有界 preview；microcompact 只替换活动模型视图中的 `content`，presentation 保持不变；provider adapter 明确忽略 presentation。Read 保留行范围语义，并额外按字符封顶、通过 `next_offset` 显式分页。
