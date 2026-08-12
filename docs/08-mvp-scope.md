# Python MVP 边界

## 目标

第一阶段先打通一条可测试的纵向链路：用户输入被持久化后，Agent 构造模型上下文，调用 Anthropic Messages API，执行受权限控制的工具，再把匹配的 `tool_result` 送回模型。

## 本阶段包含

- `nano-code` REPL 和 `nano-code -p "..."` 单次运行；
- 与 Agent Loop 解耦的模型 Provider 协议及 Anthropic 适配器；
- Read、Glob、Grep、Write、Edit、Bash 六个内置工具；
- `default`、`acceptEdits`、`plan`、`dontAsk`、`bypassPermissions` 权限模式；
- 无交互环境 fail closed，工作区文件路径防逃逸；
- 追加式 JSONL Transcript、会话恢复和 UUID 父链；
- 基于完整用户回合的上下文后缀投影，避免切断 tool-use/result 配对；
- 超大工具结果落盘并给模型返回稳定预览。

## 对照 Claude Code 保留的不变量

| 机制 | MVP 保留点 | 参考入口 |
| --- | --- | --- |
| Query Loop | 明确的模型→工具→模型循环及 `max_turns` | `src/query.ts` |
| Message | API 投影与 Transcript 分离 | `src/types/message.ts`、`utils/messages.ts` |
| Tool | schema、风险属性、执行和结果映射分层 | `src/Tool.ts`、`services/tools/toolExecution.ts` |
| Permission | deny/ask 优先，headless ask 自动拒绝 | `types/permissions.ts`、`utils/permissions/permissions.ts` |
| Context | 只按完整 API round 裁剪 | `services/compact/grouping.ts` |

当前快照的 `groupMessagesByApiRound()` 已使用 assistant response ID 建立比人类回合更细的边界。MVP 尚无摘要注入和断裂修复，因此先采用更保守的人类回合边界：它同样不会切断 API round，但单个长回合超限时会明确失败。实现 compact 时应重新对齐该分组算法。

## 延后实现

流式响应和流式工具调度、并行 safe 工具、LLM 摘要 compact、Hooks、MCP/deferred tools、规则持久化以及 OS 级 sandbox 不属于第一阶段。尤其 Bash 当前只有权限门控，不等价于进程隔离；在 sandbox 完成前不应把 `bypassPermissions` 作为默认模式。
