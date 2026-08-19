# 阶段 4：简化 Tool、Permission 与 Workspace

完成日期：2026-08-19。

## 最终边界

- `workspace` 只公开具体 `Workspace`，拥有 canonical path、符号链接边界、相对路径展示和本地 UTF-8 I/O；没有单实现 Protocol，也不解释权限规则。
- `permissions` 拥有 mode、rule、结构化 `PermissionRequest`、decision、prompt/confirmation 和更新语义。`PermissionPolicy.decide(request)` 不接收 Tool、Workspace 或回调。
- `tools` 拥有 Tool、Registry、`ToolExecutor`、调用审计、结果外置、presentation 和 ToolRound。`ToolExecutor` 直接组合具体 Tool、PermissionPolicy 与 Workspace。
- 文件规则匹配迁入 `permissions.path_rules`；ToolRound 事件和串行执行器迁入 `tools`，旧 `agent.contracts.tool`、`agent.ports.tool` 与单实现 `ToolRoundPort` 已删除。

## 受控执行顺序

每个模型 ToolCall 只能经过同一条管线：

1. Registry 解析 Tool，Tool 校验输入；
2. Tool 根据输入语义、当前 mode/rules 和 workspace root 生成局部权限结果；
3. ToolExecutor 将名称、输入和局部结果组装为 PermissionRequest；
4. PermissionPolicy 按显式 deny/ask、安全结果、bypass、allow 和默认模式裁决；
5. 审计成功后，必要时等待一次用户 confirmation；
6. Tool 只使用准确获准的输入执行，并通过 Workspace 完成有边界的文件 I/O；
7. 异常、拒绝和取消均生成或补齐匹配原 ToolCall ID 的协议结果。

审计失败会在 Tool 启动前失败关闭；permission prompt 取消会直接传播取消且不执行 Tool。Workspace 在实际读写时再次解析路径，防止授权后符号链接被替换造成边界逃逸。

## 前序阶段复核

阶段 1 的 Pyright standard 与阶段 2 的唯一 ModelClient 保持不变。阶段 3 的 Conversation/Session 所有权和持久化优先不变量未被工具重构改写。

阶段 4 删除了全部 phase-4 依赖和深层 import 例外，并清除了一个已过时的 phase-5 `features.file_mentions -> permissions` 深层 import 例外。跨模块 import 从 225 降至 209，临时违规模块边从 30 降至 27，临时深层 import 边从 36 降至 30。阶段 3 的 9 模块主循环拆为 `agent/context/features.todos/sessions/tools` 五模块环与 `core/permissions/providers` 三模块环；分别留待阶段 5–6 和阶段 7 消除。

## 验收

- `permissions` 不导入 `tools`，`workspace` 无跨模块依赖；
- `tools -> permissions/workspace`，不存在反向依赖或执行旁路；
- 拒绝、permission prompt 失败/取消、审计失败、工具异常、ToolRound 取消补齐和 workspace 符号链接逃逸均有回归测试；
- Ruff、Pyright standard、pytest 与 AST 架构守卫全部通过。
