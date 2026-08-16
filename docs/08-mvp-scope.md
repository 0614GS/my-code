# Python MVP 边界

## 目标

第一阶段先打通一条可测试的纵向链路：用户输入被持久化后，Agent 构造模型上下文，调用 Anthropic Messages API，执行受权限控制的工具，再把匹配的 `tool_result` 送回模型。

## 本阶段包含

- 带 slash 命令选择、项目会话 `/resume` 的轻量 TUI，以及 `nanocode -p "..."` 单次运行；
- 用户级 API Key 登录、状态、退出及环境变量临时覆盖；
- 与 Agent Loop 解耦的模型 Provider 协议及 Anthropic 适配器；
- Read、Glob、Grep、Write、Edit、Bash 和 TodoWrite 七个内置工具；
- 输入感知的工具权限协议、Bash 保守只读分析，以及 `default`、`acceptEdits`、`plan`、`dontAsk`、`bypassPermissions` 权限模式；
- 无交互环境 fail closed，工作区文件路径防逃逸；
- 追加式 JSONL Transcript、轻量会话发现、活动父链恢复和运行时原子切换；
- 与 Claude Code 同形的全局/项目存储布局和三级配置覆盖；
- `TranscriptEntry`、`ConversationMessage`、`ModelRequest` 和 provider wire type 分层边界；
- 分段提示词、static/session/turn 生命周期和 provider 自声明缓存能力；
- assistant usage 持久化、可观察预算、稳定 microcompact 和完整/响应式 compact；
- 超大工具结果落盘并给模型返回稳定预览。
- 基于完整 session history 投影的 TodoWrite 状态，以及采用独立 10/10 turn 阈值、
  仅保留在当前进程模型历史中的 todo reminder；
- 通过快照与提交后事件同步、支持 Ctrl+T 折叠的 TUI TodoList；
- Anthropic SSE 文本流、TUI 增量 Markdown 和工具调用状态展示；
- 底部内联权限选择及可返回模型的拒绝反馈。

## 对照 Claude Code 保留的不变量

| 机制 | MVP 保留点 | 参考入口 |
| --- | --- | --- |
| Query Loop | 明确的模型→工具→模型循环；主循环默认无限，显式 `max_turns` 返回结构化终态 | `src/query.ts` |
| Message | API 投影与 Transcript 分离 | `src/types/message.ts`、`utils/messages.ts` |
| Tool | schema、风险属性、执行和结果映射分层 | `src/Tool.ts`、`services/tools/toolExecution.ts` |
| Permission | 工具特定判断与全局裁决分层，deny/ask 优先，headless ask 自动拒绝 | `types/permissions.ts`、`utils/permissions/permissions.ts` |
| Context | 不静默裁剪，使用可重放 microcompact、显式 boundary 和摘要工作集 | `services/compact/`、`utils/messages.ts` |
| Prompt | 稳定前缀、动态片段和消息级 reminder 分层投影 | `constants/prompts.ts`、`utils/messages.ts` |

请求投影会合并相邻同 role 消息，并严格校验 tool-use/result 配对。提示词来源由 Registry 按生命周期解析；结构化 system context 只在请求边界渲染 XML，provider 自行决定是否映射缓存断点。超限不会删除旧轮次：先尝试稳定工具结果替换，再生成 compact summary；Provider 返回明确的上下文错误时最多响应式重试一次。

## 延后实现

流式文本响应已经实现，但工具仍在完整 assistant message 到达后串行调度；半截工具 JSON 不会执行或持久化。并行 safe 工具、cached microcompact、compact 后文件/plan/skill 工作集重建、Hooks、MCP/deferred tools、细粒度权限规则持久化以及 OS 级 sandbox 仍不属于当前阶段。Bash 的只读判定只减少可证明安全命令的确认次数，不等价于进程隔离；在 sandbox 完成前不应把 `bypassPermissions` 作为默认模式。
