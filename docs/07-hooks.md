# Hooks 扩展点

## 当前状态

my-code 当前没有 Hooks 运行时、配置 schema 或生产 `hooks` 包。权限、工具执行、compact 和 stop 行为均由具体模块直接实现。

不为潜在扩展预先建立 Hook port、manager 或空事件目录。

## 未来边界

如果出现真实 Hook 需求，应按事件的所有者接入：

- Pre/PostToolUse 围绕 `ToolExecutor`，但不能跳过权限策略。
- Compact 前后围绕 `ContextEngine.compact()` 与调用方的 Session commit。
- Turn stop/complete 围绕 Chat 或 Agent 的明确终态。
- Provider request/response 观察留在 provider-neutral Model 边界，不暴露 SDK event。

Hook 输入必须是稳定、最小且不含凭据的快照。Hook 输出需要使用判别联合明确表达允许的动作，例如继续、拒绝或补充上下文，不能通过任意可变字典修改内部对象。

## 安全顺序

未来 PreToolUse 可以进一步拒绝或要求确认，但不能把 PermissionPolicy 的 deny 改为 allow。PostToolUse 即使失败也不能删除已经产生的 ToolResult。Stop hook 的重试必须受明确次数限制，不能形成无限 Agent loop。

## 生命周期与失败

- Hook 注册属于应用生命周期，不属于 Session facts。
- Hook 单次执行状态属于当前事件，不跨 Turn 复用。
- 超时、取消和非零退出必须有稳定策略并可观察。
- Hook 错误默认不能破坏 Session 的已提交事实。

只有出现至少一个具体 hook 实现和配置入口后，才从共同调用形状提取 Protocol。
