# 工具、权限与工作区

所有工具能力——内置工具、feature tools、MCP adapter 和 Skill selector——最终都实现同一套 provider-neutral Tool 协议，并经过同一条校验、权限、执行和结果闭合路径。

## Catalog 与 step 快照

`AppState.tools` 持有 application-lifetime `ToolCatalog`。每个来源通过稳定 `ToolSourceId` 原子注册、替换或撤销一组工具；重名或非法更新整体失败，不静默覆盖其他来源。

Agent 在每个 step 开始时捕获完整 `ToolCatalogSnapshot`，再结合 Session discovery state 生成 `ToolExposureSnapshot`：

- `eager` 工具直接把 definition 发给模型；
- `searchable` 工具通过 ToolSearch 按需发现；
- 请求 definitions、调用校验和 ToolRound 执行都使用同一 exposure snapshot。

Catalog 在模型流或工具执行期间更新时，当前 step 不漂移，下一 step 才看到新版本。

`tools.toolSearchMode` 支持 `dispatcher` 和 `native`。Dispatcher 模式让稳定的 invoker 转发已搜索工具，真实目标仍完整经过自己的 schema、权限、审计和执行；native 模式从后续 step 暴露命中的原始 definition。两者都不把 Provider 专用 tool-reference 类型带入公共模型。

## 单次执行管线

```text
ToolCall
  -> 当前 exposure / catalog snapshot 查找
  -> Tool schema 校验与规范化输入
  -> Tool 生成局部权限分析
  -> PermissionPolicy 合并 rules 与 mode
  -> 必要时 PermissionPrompter 确认
  -> audit / hook
  -> ToolContext 中执行
  -> ToolResult(content, is_error, presentation)
  -> Session 提交完整 ToolResultBatch
```

校验后的输入分别以隔离副本传给 permission、audit、hook、presentation 和执行逻辑，观察型扩展不能原地修改持久化 ToolCall 或获准输入。`ToolExecutor` 返回完整领域结果，不绑定活动 Session 或结果目录；大结果外置和 durable presentation 由 Session 提交事务处理。

拒绝、验证失败、执行异常和取消都必须形成与原 call ID 配对的错误结果。权限更新先持久化目标配置，再替换 runtime policy；写入失败时当前权限不改变。

## ToolRound 与并行

`ToolRoundExecutor` 按 AssistantMessage 中的 ToolCall 顺序扫描：

- 连续且 `is_concurrency_safe(input)` 为真的调用组成有上限的并行组；
- 不安全调用单独执行，并成为前后屏障；
- 未知、MCP 和有副作用的工具默认不安全；
- 最终 batch 始终按原 ToolCall 顺序排列，不受完成先后影响。

取消会回收当前组的任务，保留已经完成的结果，并为其余调用补齐稳定错误结果。下一个模型 step 只能在完整 batch 成功提交后开始。

## PermissionPolicy

权限由结构化 `PermissionRequest` 决策，不接收整个 Tool 或任意回调。核心输入包括 Tool 名称、规范化参数、Tool 的安全分析、当前模式和分层规则。

显式 deny 始终优先；ask 触发 host 确认；allow 只授权匹配范围。Full access/bypass 仍不能绕过工具自身不可变的安全限制，例如 Explore 的只读代理。无 permission handler 时请求默认拒绝。

一次 pending approval 属于当前调用，完成、拒绝、异常或取消后必须释放。Runtime permission mode/rules 的唯一可写来源是 `AppState.permissions.policy`；UI 只能通过 Chat 用例更新。Session mode 变更先由 `Session` 原子写入 transcript，再发布到 policy；resume 恢复目标 Session 的最后模式，包括 `bypassPermissions`。

## Workspace 安全边界

`workspace.Workspace` 拥有路径规范化、工作区根、相对展示和具体本地 I/O，但不解释 PermissionRule。文件工具在权限分析时解析目标，在真正 I/O 前再次解析并检查边界，防止授权后符号链接替换造成逃逸。

应用层保护不是 OS sandbox。即使启用 bypass，Tool 自身的输入限制、工作区复验和 Explore read-only 约束仍生效；对不可信项目不能把它当作系统调用隔离。

## 必须保持的不变量

- 所有模型可调用能力都通过标准 Tool 和 `ToolExecutor`，不存在 MCP/Skill/Subagent 旁路。
- 同一 step 的 definitions、校验与执行来自同一不可变快照。
- Permission deny 不能被 hook、bypass 或 feature 反向升级为 allow。
- 实际执行只能使用已经校验和获准的输入。
- ToolCall 在执行前已经作为完整 AssistantMessage 持久化，每个调用最终闭合。

主要源码入口：`src/my_code/tools/catalog.py`、`src/my_code/tools/executor.py`、`src/my_code/tools/round_executor.py`、`src/my_code/permissions/policy.py`、`src/my_code/workspace/local.py`。
