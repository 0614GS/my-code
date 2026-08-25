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

用户目录在真实启动或认证操作时幂等初始化。导入模块不会创建文件、读取凭据或发起网络请求。

## Settings 合并

`SettingsStore` 按 user、project、local 顺序叠加持久配置；入口 `SettingsOverrides` 再覆盖允许的运行参数。`SettingsResolver` 结合活动 provider profile、环境变量、凭据和模型能力，生成不可变 `AgentSettings` 快照。

Provider endpoint、protocol、默认 model、reasoning、limits 和 compact 配置属于 `providers.json`。项目 settings 可以覆盖 Agent model 等运行参数，但不保存 API key。

工具调度配置位于 settings 的 `tools` 域：`tools.maxParallelCalls` 必须是正整数，默认值为 4；设为 1 会关闭 ToolRound 内的实际重叠执行。它只限制并发上限，不会把默认不安全的 Tool 改为安全。

foreground Subagent 配置位于 `subagents` 域。`enabled` 默认 `false`；`maxDepth`、`maxActiveChildren`、`maxSteps`、`maxTokens` 和 `timeoutSeconds` 都必须为正数。gate 只决定是否注册 Subagent tool source，预算值由不可变 `AgentSettings` 传给每个 child run。

`backgroundTasks.enabled` 默认 `false`，且要求 `subagents.enabled=true`。关闭它不会撤销 foreground Subagent，只移除 background 参数、Task tools 和完成通知 source。

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

`mcp.deferredToolThreshold` 必须为正整数，默认 50。单个 server 的发现结果超过阈值时，只先暴露该 server 的本地 ToolSearch；搜索命中工具从下一 step 的 catalog snapshot 开始可见。把阈值设得足够大可关闭实际 deferred 行为。

共享 project settings 可以声明 MCP server，但不能开启全局 MCP 或直接启用 server；project 定义默认 disabled。要信任并运行它，用户必须把完整定义复制到 `settings.local.json`。user/local server 可以启用；同名定义仍按 local > project > user 选择最高层完整值。

## 凭据

`auth.credentials.CredentialStore` 只处理私有凭据文件。解析顺序由 provider/protocol 和环境变量决定，并返回 `ResolvedCredential` 说明来源。任何 status、history、日志或 Session record 都不能包含 API key。

## 写入语义

- settings、profiles 和凭据使用临时文件加原子替换。
- PermissionUpdate 先写目标配置，再修改活动 PermissionPolicy。
- Session 通过同目录临时文件与原子替换提交 JSONL 事务；恢复时严格校验 schema、父链、tool pairing、compact boundary 和 replay 关联。
- MCP server 配置只生成不可变运行 spec；连接、诊断和远端工具目录不写入 Session。
- Skill index、诊断和 pending activation 只存在于 application runtime；SKILL.md 正文不会写入 Session，只有普通 `Skill` ToolCall/ToolResult 作为 conversation fact 保存。
- 大型工具结果按 session ID 分目录，由 Session 在 tool batch 提交内创建或回滚；调用方不接触 store/path。

## 版本控制

可以共享项目 `.my-code/settings.json`。不得提交 `settings.local.json`、凭据、Session JSONL、工具输出、`.venv` 或 API key。
