# 存储与配置

## 所有权

`config` 拥有路径、分层 settings、provider profile 和配置持久化。`auth` 拥有 API key 存储与解析。`sessions` 私有拥有 Conversation JSONL，并管理位于运行时临时目录的大型工具输出文件。

根 `my_code.bootstrap` 是唯一 composition root，负责初始化存储并组装运行时对象；其他模块不能导入 bootstrap。

## 本地目录

```text
~/.my-code/
├── settings.json
├── skills/<name>/SKILL.md
├── providers.json
├── .credentials.json
├── .model-catalog.json
└── projects/<workspace-key>/
    └── <session-id>.jsonl

<workspace>/.my-code/
├── settings.json
├── settings.local.json
└── skills/<name>/SKILL.md
```

大型工具结果与后台 task 输出统一位于系统临时目录：

```text
<tmp>/my-code-<uid>/<workspace-key>/<session-id>/
├── tool-results/<tool-use-digest>.txt
└── tasks/<task-id>.output
```

用户目录在真实 TUI 启动时幂等初始化。导入模块不会创建文件、读取凭据或发起网络请求。launch 分支要求 stdin 和 stdout 都是 TTY，并在存储初始化、Provider 发现或网络访问前拒绝管道与重定向；`--help`、`--version` 仍可在任意终端环境使用。未来配置类子命令可以独立允许非 TTY，但不改变 launch 边界。

## Settings 合并

`SettingsStore` 按 user、project、local 顺序叠加持久配置；入口 `SettingsOverrides` 只能临时选择一个已存在的 Provider profile，不能覆盖 profile 内容。`SettingsResolver` 结合活动 profile、私有凭据和模型能力，生成不可变 `AgentSettings` 快照。

Provider protocol、Base URL、默认 model、reasoning、limits 和 compact 配置只属于 `providers.json`。API Key 只属于 `.credentials.json`，当前选择只属于用户级 `settings.json.activeProvider`。新用户的 `providers` 目录为空，`settings.json` 不写 `activeProvider`；首次启动必须先完成向导。`agent.model`、`--model` 和 `--base-url` 已删除，旧 `agent.model` 会返回迁移错误，不能由项目 settings 覆盖模型。

最小 `~/.my-code/providers.json` 如下；`reasoning`、`limits` 和 `compact` 可省略：

```json
{
  "version": 3,
  "providers": {
    "openai": {
      "protocol": "openai-responses",
      "defaultModel": "gpt-5.4"
    }
  }
}
```

对应的 `~/.my-code/settings.json`：

```json
{"version": 3, "activeProvider": "openai"}
```

空 `baseUrl` 分别使用 `https://api.anthropic.com` 和 `https://api.openai.com/v1`。自定义网关可以不保存 Key；官方 Endpoint 缺少 Key 会在在线探查时报告认证失败。

工具调度配置位于 settings 的 `tools` 域：`tools.maxParallelCalls` 必须是正整数，默认值为 4；设为 1 会关闭 ToolRound 内的实际重叠执行。它只限制并发上限，不会把默认不安全的 Tool 改为安全。

foreground Subagent 配置位于 `subagents` 域。`enabled` 默认 `true`；`maxDepth=3`、`maxActiveChildren=4` 是默认结构限制。`maxSteps`、`maxTokens` 和 `timeoutSeconds` 缺省时不限制，显式配置时必须为正数；这些可选预算由不可变 `AgentSettings` 传给每个 child run。`Subagent` 是基础 eager tool，不经 ToolSearch 延迟加载，并允许多个独立 child 调用在全局 `tools.maxParallelCalls` 范围内并发执行。

`backgroundTasks.enabled` 默认 `true`，可独立于 `subagents.enabled` 关闭。它只在 interactive effective gate 下生效；关闭后不会撤销 foreground Subagent，而会移除 Bash/Subagent 的 background 参数、Task tools 和完成通知 source。TaskList/TaskCancel 注册后属于 searchable tool，仍通过 ToolSearch 按需发现。

后台 Bash 的合并输出位于上述 session 临时空间。目录权限为 `0700`、文件为 `0600`。前台完成后立即删除文件；后台终态文件和外置的大型工具结果暂时保留，由 OS 临时目录生命周期回收，不写入项目 `.my-code` 或 durable session storage。Transcript 只持久化大型结果的有界 preview 与临时路径，因此系统清理后不保证恢复完整原始输出。

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

`auth.credentials.CredentialStore` 只处理私有凭据文件，并返回 `stored` 或 `none` 来源。`MY_CODE_PROVIDER`、`MY_CODE_API_KEY`、`ANTHROPIC_*` 和 `OPENAI_*` 不参与 Provider、模型、URL 或 Key 解析；SDK client 也总是收到显式 Key 和显式解析后的 URL，不能自行回读这些环境变量。Provider TUI 保存和删除都只操作目标 profile 的本地 Key；删除当前 Provider 的 Key 后，runtime 会立即重新解析并切换连接。任何 status、history、日志、探查结果或 Session record 都不能包含 API Key。

在线 `probe()` 每次只调用对应协议的模型目录 API，不使用缓存兜底，也不发送可能计费的生成请求。成功仅表示 Endpoint、认证和目录 API 可用，不保证所选模型一定支持 Messages/Responses 生成。启动期能力解析仍可使用无凭据缓存或本地能力；探查失败时可重试、编辑连接或手工输入模型，手工模型会明确标记为“Connection not verified”。未保存的探查不写 profile、凭据或缓存。

## 写入语义

- settings、profiles 和凭据使用临时文件加原子替换。
- PermissionUpdate 先写目标配置，再修改活动 PermissionPolicy。
- Session 通过同目录临时文件与原子替换提交 JSONL 事务；恢复时严格校验 schema、父链、tool pairing、compact boundary 和 replay 关联。
- MCP server 配置只生成不可变运行 spec；连接、诊断和远端工具目录不写入 Session。
- Skill index和诊断只存在于 application runtime；成功激活的 SKILL.md 正文作为 durable AttachmentMessage 写入 Session，供 resume/compact 恢复。
- 大型工具结果按 session ID 分目录写入系统临时空间，由 Session 在 tool batch 提交内创建或回滚；调用方不接触 store/path。

## 版本控制

可以共享项目 `.my-code/settings.json`。不得提交 `settings.local.json`、凭据、Session JSONL、工具输出、`.venv` 或 API key。
