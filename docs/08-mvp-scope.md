# Python MVP 边界

## 目标

第一阶段先打通一条可测试的纵向链路：用户输入被持久化后，Agent 构造模型上下文，通过 Anthropic Messages 或 OpenAI Responses 调用模型，执行受权限控制的工具，再把匹配的工具结果送回模型。

## 本阶段包含

- 带 slash 命令选择、项目会话 `/resume` 的轻量 TUI，以及 `nanocode -p "..."` 单次运行；
- 用户级 API Key 登录、状态、退出及环境变量临时覆盖；
- 与 Agent Loop 解耦的模型 Provider 协议，以及 Anthropic Messages、OpenAI Responses stateless 适配器；
- Read、Glob、Grep、Write、Edit、Bash 和 TodoWrite 七个内置工具；
- 输入感知的工具权限协议、Bash 保守只读分析，以及 `default`、`acceptEdits`、`plan`、`dontAsk`、`bypassPermissions` 权限模式；
- 无交互环境 fail closed，工作区文件路径防逃逸；
- 追加式 JSONL Transcript、轻量会话发现、活动父链恢复和运行时原子切换；
- 与 Claude Code 同形的全局/项目存储布局和三级配置覆盖；
- `TranscriptEntry`、`ConversationMessage`、`ModelRequest` 和 provider wire type 分层边界；
- 分段提示词、static/session/request 生命周期和 provider 自声明缓存能力；
- assistant usage 持久化、可观察预算、稳定 microcompact 和完整/响应式 compact；
- 超大工具结果落盘并给模型返回稳定预览。
- 基于完整 session history 投影的 TodoWrite 状态，以及采用独立 10/10 completed-model-call 阈值、
  仅保留在当前进程模型历史中的 todo reminder；
- 区分请求派生与回合事件交付的 attachment 基础设施，支持 `request` / `live_session`
  retention、消息锚点、user-side 文本/reminder/observation 投影和窗口计量；
- 通过快照与提交后事件同步、支持 Ctrl+T 折叠的 TUI TodoList；
- 请求内连续编号、单活动块的文本/reasoning 生命周期、TUI 原子收口的增量
  Markdown/折叠 reasoning 和工具调用状态展示；
- 底部内联权限选择及可返回模型的拒绝反馈。
- settings 中的 `permissions.allow/deny/ask` 持久规则、结构化 PermissionUpdate、
  local settings 级“不再询问”和标准库权限审计日志。
- v3 settings、v5 Transcript、v3 provider/v2 credential 存储，以及带启动快照与 metadata 的 Session；
- 四种 reasoning disclosure、隐藏 continuation，以及 protocol/profile/model/endpoint 绑定。

## 对照 Claude Code 保留的不变量

| 机制 | MVP 保留点 | 参考入口 |
| --- | --- | --- |
| Query Loop | 明确的模型→工具→模型循环；主循环默认无限，显式 `max_steps` 返回结构化终态 | `src/query.ts` |
| Message | API 投影与 Transcript 分离 | `src/types/message.ts`、`utils/messages.ts` |
| Tool | schema、风险属性、执行和结果映射分层 | `src/Tool.ts`、`services/tools/toolExecution.ts` |
| Permission | 工具特定判断与全局裁决分层，deny/ask 优先，headless ask 自动拒绝 | `types/permissions.ts`、`utils/permissions/permissions.ts` |
| Context | 不静默裁剪，使用可重放 microcompact、显式 boundary 和摘要工作集 | `services/compact/`、`utils/messages.ts` |
| Prompt | 稳定前缀、动态片段和消息级 reminder 分层投影 | `constants/prompts.ts`、`utils/messages.ts` |

请求投影会合并相邻同 role 消息，并严格校验 tool-use/result 配对。提示词来源由 Registry 按生命周期解析；结构化 system context 只在请求边界渲染 XML，provider 自行决定是否映射缓存断点。超限不会删除旧历史：先尝试稳定工具结果替换，再生成 compact summary；Provider 返回明确的上下文错误时最多响应式重试一次。

## Bash 执行边界

Bash 工具正式限定为提供 `/bin/bash` 的 POSIX 环境，并固定以 `/bin/bash -c` 执行；
不会回退到 `/bin/sh`、zsh、PowerShell 或用户默认 shell。子进程环境移除 provider
凭据、`BASH_ENV`、`ENV`、`SHELLOPTS`、`BASHOPTS`、`CDPATH` 与导出的 Bash
函数。tree-sitter AST 只用于保守权限分析，分析失败表示 ask，不提供进程或文件系统
隔离。

## 延后实现

工具仍在完整 assistant message 到达后串行调度；半截工具 JSON 或 continuation 不会执行或持久化。流式层不实现并行活动展示块、断线续传或以 provider sequence 作为 UI 身份；OpenAI 交错 output item 由 adapter 缓冲后按 output 顺序交付。不实现 OpenAI Chat Completions 及第三方 `reasoning_content` 扩展。TUI 已支持工作区内 UTF-8 文本文件和目录的 `@` 补全与临时 attachment；媒体 attachment、并行 safe 工具、Session fork、`state.json`、OAuth/Keychain、cached microcompact、Hooks、MCP/deferred tools 以及 OS 级 sandbox 仍不属于当前阶段。
