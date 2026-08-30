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

`Edit` 和 `Write` 在文件写入成功后，根据本次写入前后的 UTF-8 内容生成结构化
`FileDiffPresentation`。它只描述该次工具调用，不读取 Git 状态；完整增删统计与最多
200 条的展示记录分离。比较采用 3 行上下文，展示超限时优先裁剪上下文并保留变更首尾；
任一侧超过 2 MiB 或 50,000 行时跳过高成本比较，记录明确的省略原因。失败、权限拒绝
和取消结果不携带 diff。

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

权限提示由 application-lifetime prompter 跨 Agent 串行处理；runtime 关闭会取消正在展示和排队的请求。Sandbox 越界提示属于独立类别，展示请求 Agent/run、完整命令、justification 和宿主权限风险，只允许单次批准或拒绝，不产生可记忆规则。

## Workspace 与命令执行边界

`workspace.Workspace` 拥有路径规范化、工作区根、相对展示和具体本地 I/O，但不解释 PermissionRule。文件工具在权限分析时解析目标，在真正 I/O 前再次解析并检查边界，防止授权后符号链接替换造成逃逸。

`workspace.Workspace` 与 `workspace.CommandLauncher` 职责独立。前者约束文件工具路径；后者统一承载前台 Bash、后台 Bash 以及子 Agent 的 Bash。MCP server、Provider 和 my-code 主进程仍在宿主运行。

Linux 默认以 `auto` 模式在启动时探测 system Bubblewrap。探测成功后，每条 Bash 命令拥有独立的 user/PID/IPC/network namespace，宿主根文件系统只读、当前 workspace 可写，`.git` 与 `.my-code` 重新只读挂载，且只有命令私有的 `TMPDIR` 是额外可写位置。工作区使用 bind mount，不复制文件，写入会立即反映到宿主。

启动探测失败会发出带原因的警告并冻结为 local backend；显式 `sandbox.mode=local` 不视为 fallback。Bubblewrap 一旦探测成功，后续单次启动失败直接形成工具错误，不以 local 重试。默认网络隔离可由可信的 User 或 Local settings 设置为 enabled；Project settings 无权修改 sandbox。

真实 Bubblewrap backend 下，普通 Bash 在显式 deny/ask 和 Plan mode 检查之后由 sandbox 自动放行，AST 不再承担工作区内普通执行的安全证明；local 与 fallback backend 仍执行保守命令分析。若 `sandbox.allowUnsandboxedCommands` 开启，交互式 Bash schema 才暴露 `require_escalated` 和必填 justification。该请求在 `dontAsk`、Plan、无交互 frontend 或配置关闭时拒绝；Default、Approve edits 和 Full access 均必须逐次询问。批准后同一命令树通过宿主 launcher 执行，网络限制不再适用，且不会自动重试普通 sandbox 失败的命令。

启动时不存在的 `.git`、`.my-code` 使用跨进程锁和 inode 校验的空占位挂载。最后一个命令退出后仅删除身份不变的空目录；非空、符号链接或身份变化会保留内容并报告 policy violation。

Bubblewrap 是 Bash 的 OS 级挂载与 namespace 边界，但不是整个应用的系统调用 sandbox，也不包含 seccomp、域名代理或 MCP 隔离。应用层 PermissionPolicy、Workspace 复验和 Explore read-only 约束仍先于命令执行，不能被 sandbox 替代。

## 必须保持的不变量

- 所有模型可调用能力都通过标准 Tool 和 `ToolExecutor`，不存在 MCP/Skill/Subagent 旁路。
- 同一 step 的 definitions、校验与执行来自同一不可变快照。
- Permission deny 不能被 hook、bypass 或 feature 反向升级为 allow。
- 实际执行只能使用已经校验和获准的输入。
- ToolCall 在执行前已经作为完整 AssistantMessage 持久化，每个调用最终闭合。

主要源码入口：`src/my_code/tools/catalog.py`、`src/my_code/tools/executor.py`、`src/my_code/tools/round_executor.py`、`src/my_code/permissions/policy.py`、`src/my_code/workspace/local.py`、`src/my_code/workspace/launcher.py`。
