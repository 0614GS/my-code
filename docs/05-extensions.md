# 扩展能力

my-code 不为每类扩展建立一套新的执行框架。动态能力以 Tool source 接入 `ToolCatalog`；需要独立生命周期的组件由 ApplicationRuntime 持有，面向模型的产物仍进入标准 Tool、Context 和 Session 路径。

## 统一扩展模型

```text
Builtin / Feature / MCP / Skill sources
        -> versioned ToolCatalog
        -> step ToolExposureSnapshot
        -> ModelRequest definitions
        -> ToolRoundExecutor / ToolExecutor
        -> ToolResultBatch / AttachmentMessage
```

来源更新只能原子影响后续 step。扩展不得向 Provider 公共模型泄漏自身 wire 类型，也不得绕过权限、取消或 Session 提交。

## MCP

`McpRuntime` 拥有 stdio transport、server 连接、发现、诊断、refresh task 和 server tool source。每个 server 完整发现成功后才原子发布工具；失败或 schema 冲突不留下半套目录。

MCP Tool adapter 只负责 schema 和调用协议转换，执行时仍是普通 Tool。远端 ToolCall/ToolResult 进入 Conversation；连接状态、诊断和 discovery cache 不进入 Session。`tools/list_changed` 由受管 refresh task 合并处理，runtime 关闭会取消并等待这些任务再回收 transport。

项目 settings 中的 server 定义默认不受信任：project 层不能直接开启全局 MCP 或 server，用户必须在 local settings 中显式启用完整定义。环境变量只通过名称映射注入，不把值写入配置、诊断或 Session。

当前支持 stdio tools；resources、prompts、富媒体结果和更新版 driver 仍延后。

## Skills

`SkillRuntime` 分层发现 project、user、builtin 及规范化外部来源，并用 `SkillCatalog` 原子发布索引。Discovery 只读取选择所需元数据和 fingerprint；执行 `Skill` Tool 时才重新读取并验证 `SKILL.md` 正文，目录中的 Python 或 shell 不会被导入或执行。

成功激活后，Skill 正文作为 durable `AttachmentMessage` 放在完整 ToolResultBatch 之后，不修改 system prompt。`allowed-tools` 被解析为 additive session allow rules；deny/ask 仍优先，且规则不会隐藏 ToolCatalog 中的能力。reload 不改变当前 ToolRound 持有的旧 snapshot。

## Subagent 与 AgentRun

`AgentRunFactory` 为 child 创建独立 Run ID、Session、Agent 组件、`SessionContextCache`、child-local ToolCatalog 和 provider lease。child Session header 持久化 subagent kind、parent Session、创建它的 parent Run 与 agent name；Session ID 不复用 Run ID。父级只传显式 prompt/attachments，child transcript 不合并到父 Session，Session catalog 也不会把它列入 `/resume`。

Foreground 与 child 共享同一套 Session/Run 身份语义，但资源 capsule 不强行一致：每次
foreground Session activation 生成独立 Run ID，并随 `ActiveSessionBinding` 发布；它不建立
拥有 provider lease 的 `AgentRun`。Child 必须使用 `AgentRun` capsule，因为它需要独立
Agent 组件、lease 和 close 行为。parent Session 表达持久化归属，parent Run 表达本次进程
内执行来源，两者不得合并成一个含混字段。

内置角色为 `explore` 与 `general`：

- Explore 是只读叶子，只继承父 step 中的 Read、Glob、Grep 和 Bash，并在权限判断与执行前分别复验底层 `is_read_only()`。
- General 继承创建时父快照中的可用能力；嵌套时重新绑定 child identity，并受最大深度限制。

角色只能收窄父权限，不能扩大。Provider 切换只影响之后创建的 lease；现有 run 保持自己的 client、stream lock 和 binding。

## 后台任务

`TaskSupervisor` 提供进程内任务状态机、父子取消树和有限终态快照。Bash 与 Subagent 通过 background feature 复用 owner/delivery 控制面；TaskList/TaskCancel 仍是标准、可搜索 Tool。

后台提交先返回 task ID。registry 与输出目录都按 root Session identity 隔离，live Run ID 只用于执行 lineage；切换前台 Session 不会把旧 Session 的完成结果投递到新 Session。完成状态通过无 payload 的 wake signal 提醒交互 host，实际结果随后从 registry 拉取，并以 task ID 幂等地提交 durable attachment；Session 接受后才 acknowledge。任务进度、registry 和 wake signal 都不是 Conversation facts。

首版不承诺进程重启后继续运行。正常关闭会取消并等待任务终止；异常恢复不会把旧任务伪装为仍在运行。

## Hooks

当前没有通用 Hooks runtime、配置 schema 或生产 `hooks` 包。工具执行只保留已有的受控 hook callback；不会为未来事件预建全局 manager。

若引入新的 Hook，必须从真实调用点提取最小、无凭据、不可变的输入，并遵守：PreToolUse 只能进一步拒绝或要求确认，不能覆盖 PermissionPolicy deny；PostToolUse 失败不能删除已产生结果；Stop/Compact hook 必须有明确超时、取消和重试上限。

主要源码入口：`src/my_code/mcp/runtime.py`、`src/my_code/skills/runtime.py`、`src/my_code/features/subagents/controller.py`、`src/my_code/runtime/runs.py`、`src/my_code/tasks/supervisor.py`。
