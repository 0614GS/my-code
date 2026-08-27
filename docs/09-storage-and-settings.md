# 存储与配置

## 所有权

`config` 拥有路径、分层 settings、provider profile 和配置持久化。`auth` 拥有 API key 存储与解析。`sessions` 私有拥有 Conversation JSONL 与大型工具输出文件。

根 `my_code.bootstrap` 是唯一 composition root，负责初始化存储并组装运行时对象；其他模块不能导入 bootstrap。

## 本地目录

```text
~/.my-code/
├── settings.json
├── skills/<name>/SKILL.md
├── providers.json
├── .credentials.json
├── model-cache/
└── projects/<workspace-key>/
    ├── sessions/<session-id>.jsonl
    └── tool-results/<session-id>/...

<workspace>/.my-code/
├── settings.json
├── settings.local.json
└── skills/<name>/SKILL.md
```

用户目录在真实 TUI 启动时幂等初始化。导入模块不会创建文件、读取凭据或发起网络请求。launch 分支要求 stdin 和 stdout 都是 TTY，并在存储初始化、Provider 发现或网络访问前拒绝管道与重定向；`--help`、`--version` 仍可在任意终端环境使用。未来配置类子命令可以独立允许非 TTY，但不改变 launch 边界。

## Settings 合并

`SettingsStore` 按 user、project、local 顺序叠加持久配置；入口 `SettingsOverrides` 再覆盖允许的运行参数。`SettingsResolver` 结合活动 provider profile、环境变量、凭据和模型能力，生成不可变 `AgentSettings` 快照。

Provider endpoint、protocol、默认 model、reasoning、limits 和 compact 配置属于 `providers.json`。项目 settings 可以覆盖 Agent model 等运行参数，但不保存 API key。

工具调度配置位于 settings 的 `tools` 域：`tools.maxParallelCalls` 必须是正整数，默认值为 4；设为 1 会关闭 ToolRound 内的实际重叠执行。它只限制并发上限，不会把默认不安全的 Tool 改为安全。

foreground Subagent 配置位于 `subagents` 域。`enabled` 默认 `true`；`maxDepth=3`、`maxActiveChildren=4` 是默认结构限制。`maxSteps`、`maxTokens` 和 `timeoutSeconds` 缺省时不限制，显式配置时必须为正数；这些可选预算由不可变 `AgentSettings` 传给每个 child run。`Subagent` 是基础 eager tool，不经 ToolSearch 延迟加载，并允许多个独立 child 调用在全局 `tools.maxParallelCalls` 范围内并发执行。

`backgroundTasks.enabled` 默认 `true`，可独立于 `subagents.enabled` 关闭。它只在 interactive effective gate 下生效；关闭后不会撤销 foreground Subagent，而会移除 Bash/Subagent 的 background 参数、Task tools 和完成通知 source。TaskList/TaskCancel 注册后属于 searchable tool，仍通过 ToolSearch 按需发现。

后台 Bash 的合并输出位于系统临时目录下的用户/项目/session 隔离空间：`my-code-<uid>/<project>/<session>/tasks/<uuid>.output`。目录权限为 `0700`、文件为 `0600`。前台完成后立即删除文件；后台终态文件暂时保留，由 OS 临时目录生命周期回收，不写入项目 `.my-code` 或 durable session storage。

Skill 配置位于 `skills` 域，`enabled` 默认 `false`。开启后按 project `.my-code/skills` > user config `skills` > package builtin 搜索 `<name>/SKILL.md`；同层同名不会按遍历顺序覆盖，而是隔离并产生诊断。当前 frontmatter 只支持 `name`、`description`、`allowed-tools` 和 `compatibility` 的平面字段，不导入目录中的 Python 或执行 shell。关闭 gate 不扫描目录，也不注册 `Skill` Tool。

```json
{
  "version": 3,
  "skills": {"enabled": true}
}
```

```markdown
---
name: code-review
description: Review a change for correctness and regressions
allowed-tools: [Read, Glob, Grep]
compatibility: my-code
---
Inspect the relevant implementation and tests before reporting findings.
```

`name` 可省略并默认使用目录名；`description` 与非空正文必填。`allowed-tools` 未声明表示不额外限制，显式 `[]` 表示下一 step 不提供任何工具。

MCP 配置位于 `mcp` 域，`enabled` 默认 `false`。每个 `servers.<name>` 是完整替换的 server 定义，包含 `command`、`args`、`envFrom`、`enabled` 与启动/调用 timeout。`envFrom` 只保存“目标变量名 → 来源变量名”，不保存值，也不支持字面量 `env`。

`tools.toolSearchMode` 接受 `dispatcher` 或 `native`，默认 `dispatcher`，不提供 CLI/TUI 切换。旧 `mcp.deferredToolThreshold` 已删除并作为未知配置立即失败。Discovery 记录属于 Session，切换 provider 不会清空。

共享 project settings 可以声明 MCP server，但不能开启全局 MCP 或直接启用 server；project 定义默认 disabled。要信任并运行它，用户必须把完整定义复制到 `settings.local.json`。user/local server 可以启用；同名定义仍按 local > project > user 选择最高层完整值。

## 凭据

`auth.credentials.CredentialStore` 只处理私有凭据文件。解析顺序由 provider/protocol 和环境变量决定，并返回 `ResolvedCredential` 说明来源。Provider TUI 显示 `environment`、`stored` 或 `not configured`，保存和删除都只操作目标 profile 的本地 Key；环境变量始终优先且不会被删除。删除当前 Provider 的本地 Key 后，runtime 会立即重新解析并切换连接。任何 status、history、日志或 Session record 都不能包含 API key。

## 写入语义

- settings、profiles 和凭据使用临时文件加原子替换。
- PermissionUpdate 先写目标配置，再修改活动 PermissionPolicy。
- Session 通过同目录临时文件与原子替换提交 JSONL 事务；恢复时严格校验 schema、父链、tool pairing、compact boundary 和 replay 关联。
- MCP server 配置只生成不可变运行 spec；连接、诊断和远端工具目录不写入 Session。
- Skill index和诊断只存在于 application runtime；成功激活的 SKILL.md 正文作为 durable AttachmentMessage 写入 Session，供 resume/compact 恢复。
- 大型工具结果按 session ID 分目录，由 Session 在 tool batch 提交内创建或回滚；调用方不接触 store/path。

## 版本控制

可以共享项目 `.my-code/settings.json`。不得提交 `settings.local.json`、凭据、Session JSONL、工具输出、`.venv` 或 API key。
