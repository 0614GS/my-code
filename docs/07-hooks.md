# Hooks

## 1. Hooks 是生命周期扩展点

Hooks 不替代 Agent Loop、Permission 或 Tool。它们在固定阶段接收结构化输入，并返回结构化影响：

| 阶段 | 时机 | 可产生的主要影响 |
| --- | --- | --- |
| `PreToolUse` | Schema/语义校验后、权限和执行前 | 修改输入、allow/ask/deny、附加上下文、阻止继续 |
| `PermissionRequest` | 操作需要授权时 | 在 headless 场景代替 UI 给出 allow/deny 与规则更新 |
| `PostToolUse` | 工具成功后 | 附加上下文、修改 MCP 输出、报告 blocking error |
| `PostToolUseFailure` | 工具异常后 | 附加诊断上下文、报告 blocking error |
| `Stop` | 模型未请求工具、准备结束时 | 允许结束、阻止结束或注入错误后重试 |
| `StopFailure` | 回合以 API/运行错误结束时 | 处理失败通知，不推动正常继续逻辑 |
| `PreCompact` | 生成摘要前 | 增补 compact instructions、提供展示消息 |
| `PostCompact` | compact 成功后 | 清理或附加 compact 后信息 |
| `SessionStart` | 新会话或 compact 后重建 | 重新注入会话级上下文 |
| `PostSampling` | 一次模型采样完成后 | 异步观察采样结果，不阻塞主路径 |

工具相关的适配层位于 `claude-code/src/services/tools/toolHooks.ts`；底层 hook 加载和执行位于 `claude-code/src/utils/hooks.ts`。

## 2. PreToolUse 输出

PreToolUse 结果被拆成独立事件处理：

- progress 或 attachment message；
- `permissionBehavior`；
- `updatedInput`；
- `additionalContext`；
- `preventContinuation` 和 `stopReason`；
- hook cancel/error。

只返回 `updatedInput` 而不返回 permission behavior，表示“修改输入后继续正常权限流程”。返回 ask 时，hook 的消息和输入会作为强制询问内容交给交互权限层。

## 3. Hook 不能破坏权限优先级

`resolveHookPermissionDecision()` 是 Hook 与 Permission 的唯一汇合点：

```text
hook allow
  → 再检查 blanket deny / ask、tool-specific rule 和 safety check
  → 无冲突时跳过普通交互

hook ask
  → 带 hook message / updatedInput 进入 canUseTool

hook deny
  → 直接拒绝工具
```

需要真实用户交互的工具仍会调用 `canUseTool`；只有 hook 同时提供了满足交互需要的 updated input 时，才可视为已完成该交互。详细优先级见 [05-permission-system.md](05-permission-system.md)。

## 4. 工具成功与失败 Hook

工具成功后，原始结果先被保留，再运行 PostToolUse：

- 普通内置工具先生成 tool result，hook 追加 attachment。
- MCP hook 可以返回 `updatedMCPToolOutput`，最终映射使用修改后的远端输出。
- hook 的 blocking error 会转换成结构化 attachment，供 Agent Loop 决定是否停止继续。

工具抛错时走 PostToolUseFailure；hook 自身失败会被记录并转成 hook error attachment，不应覆盖原工具错误。

## 5. Stop Hook 与重试

当模型没有返回 tool use 时，Agent Loop 才运行 Stop hooks。结果分两类：

- `preventContinuation`：直接结束，不再调用模型。
- `blockingErrors`：把错误作为新消息加入上下文，再进入下一次迭代。

`stopHookActive` 会跨迭代保存，避免同一 Stop hook 无限制自触发。API error 没有有效模型回答，因此不走普通 Stop hook；它使用 StopFailure 路径并终止，防止错误重试死循环。

实现位于 `claude-code/src/query/stopHooks.ts` 和 `claude-code/src/query.ts`。

## 6. Compact Hook

PreCompact 可以把 hook instructions 与用户手工 compact instructions 合并；用户内容在前，hook 内容在后。摘要成功后：

1. 重建工作集附件。
2. 运行 `SessionStart(compact)`，恢复会话级注入。
3. 运行 PostCompact，提供摘要文本和 trigger 类型。

这三个阶段不能折叠：PreCompact 改变摘要输入，SessionStart 重建新上下文，PostCompact 处理成功后的副作用。

## 7. 匹配、取消与可观察性

Hook 可以按工具名和工具特定内容匹配。Tool 可通过 `preparePermissionMatcher()` 预编译复杂 pattern，避免每条 hook 重复解析 shell 命令。

Hook 执行接收与当前 query/tool 关联的 abort signal。进度作为 `ProgressMessage` 发出；额外上下文和决策作为 attachment 写入消息流，使 SDK、UI 和后续模型调用能够看到一致结果。

权限决定保留 hook name、source 和 reason；工具 span 还分别记录 hook、等待用户和实际执行耗时，避免把用户思考时间误算成工具耗时。

## 8. 核心不变量

1. Hook 只能在声明的生命周期阶段产生结构化影响。
2. hook allow 不能绕过显式 deny、ask 和安全检查。
3. updated input 必须继续经过剩余校验，不能直接送入 `Tool.call()`。
4. Post hook 失败不能吞掉原始工具结果或工具异常。
5. Stop hook 重试必须携带状态并有防循环措施。
6. API error 与正常模型停止使用不同 hook 路径。

## 9. 主要源码入口

- `claude-code/src/services/tools/toolHooks.ts`
- `claude-code/src/utils/hooks.ts`
- `claude-code/src/query/stopHooks.ts`
- `claude-code/src/services/compact/compact.ts`
- `claude-code/src/types/hooks.ts`

## 10. nano-code 的当前边界

nano-code 尚未实现 Hook 调度器。已完成的 attachment 基础设施是后续 Hook
输出与 Agent 上下文之间的承载边界：基于快照的状态提醒进入
`DerivedAttachmentSource`，某个生命周期事件直接产生的 additional context 进入
`AgentTurnInput` 或未来同类的事件交付口。Hook 仍不应直接写 Transcript 或绕过
`AttachmentProjector` 拼装 provider 消息。
