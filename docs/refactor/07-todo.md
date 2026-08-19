# 重构执行 TODO

重构按阶段提交，每个阶段保持可运行、可测试。除第 0 阶段建立基线外，不接受只移动文件却保留双重权威入口的提交。

## 0. 建立基线

- [x] 将本目录评审为本轮重构的权威设计。
- [x] 将 `03-state-lifecycle.md` 的状态表转成恢复、切换和失效测试清单。
- [x] 记录完整测试、Ruff 和现有类型检查结果。
- [x] 建立 `tests/architecture` AST 扫描器。
- [x] 写入目标允许表和当前临时违规清单。
- [x] 确认临时违规不会被新代码增加。

验收：架构测试能够报告依赖边、深层 import、循环路径和技术泄漏。

## 1. 统一使用 Pyright

- [x] dev dependency 只保留 `pyright` 类型检查器。
- [x] 删除旧检查器配置，增加 `[tool.pyright]` standard 配置。
- [x] 修复 `src` 和 `tests` 中的 Pyright 错误。
- [x] 更新 README、AGENTS、CI 和其他开发命令。
- [x] 更新 lockfile。

验收：仓库不再引用旧检查器，`uv run pyright` 无错误。

## 2. 建立 `model`

- [x] 将 provider-neutral 请求、输出、事件、usage 和 capabilities 迁入 `model`。
- [x] 合并现有 Model contract 与 Model port。
- [x] 只保留一个 `ModelClient` Protocol。
- [x] 让 OpenAI、Anthropic 和 Context 依赖 `model` 的公开 API。
- [x] 删除 Agent 下旧的 Model port/contract。

验收：`providers -> model <- agent/context`，`model` 不依赖任何上层模块。

## 3. 收拢 Conversation 与 Session

- [x] 建立独立 `Conversation` 聚合，拥有 history、working set 和内存不变量，但不做 I/O。
- [x] 明确 message、ToolCall、ToolResult 和 compact 事实的唯一类型；attachment/delivery 只归 Context。
- [x] 从 Conversation 中移除 Application presentation。
- [x] 建立具体 `Session`，持有 Conversation 并执行持久化优先提交。
- [x] 将 JSONL record、codec、store、catalog 和恢复修复收拢到 `sessions`。
- [x] 将 Tool presentation 拆为 Session 附加 record/index，不作为 Conversation canonical fact。
- [x] 删除 `SessionRepository` Protocol 和 Agent-owned session contract。
- [x] 保持现有 Transcript schema，或用单独决策记录有意变更。

验收：`sessions -> conversation`；Conversation 不依赖 Session、Agent、Chat 或 UI；写盘失败不改变内存，resume 失败不替换当前 Session。

## 4. 简化 Workspace、Permission 与 Tool

- [x] Workspace 只负责路径规范化、边界和具体 I/O。
- [x] PermissionPolicy 改为接收结构化 PermissionRequest。
- [x] 从 Permissions 移除对 Tool 具体类型的依赖。
- [x] 从 Workspace 移除 PermissionRule 匹配。
- [x] ToolExecutor 直接组合 Tool、PermissionPolicy 和 Workspace。
- [x] 将通用 Tool presentation 迁入 `tools`。（阶段 3 为解除 Conversation/Chat 反向依赖提前完成）
- [x] 保持拒绝、取消、审计失败和工具异常测试通过。

验收：`tools -> permissions/workspace`，不存在反向依赖和执行旁路。

## 5. 整理 Context 与 Features

- [x] Context 只负责 Conversation 到 ModelRequest 的投影和预算管理。
- [x] 分开无状态 `ContextBuilder` 与每个活动 session 的 `ContextSession`。
- [x] live-session delivery 和 session 级 cache 在 resume/switch 时清空，request 状态在调用后丢弃。（resume 与 cache 已覆盖；完整 bundle switch 由阶段 6 验收）
- [x] attachments 与 compaction 按生命周期拆分，不建立泛化 `types.py`。
- [x] Todo 的模型、投影、reminder 和 Tool 实现由 `features.todos` 拥有。
- [x] File mention 的解析、加载和建议由 `features.file_mentions` 拥有。
- [x] 基础模块不导入具体 feature；由 bootstrap 注册 feature 能力。
- [x] 尚未实现的 Subagent 和 Plan Mode 不提前搭空框架。

验收：每个 feature 是独立依赖节点；Context 和 Tools 不反向依赖 feature；Context 不保存第二份 Conversation history。

## 6. 简化 Agent 与 Chat

- [ ] AgentEngine 直接使用 ModelClient、Context、ToolExecutor 和调用方传入的 Session。
- [ ] 删除 inbound/outbound、contract/port 和单实现 adapter 转发层。
- [ ] Agent state 仅表示一次 turn/step 的运行状态，AgentEngine 不持有活动 Session。
- [ ] 将 `application.chat` 简化为顶层 `chat`。
- [ ] 建立具体 `ChatService`，不保留单实现 `ChatRuntime` Protocol。
- [ ] Chat 原子持有并切换 Session、ContextSession 和 session tool-result binding。
- [ ] 将 session catalog、resume 和恢复历史投影从 Agent 移到 Chat/Sessions。
- [ ] TUI 和 CLI 只通过 Chat 的公开 API 驱动对话。
- [ ] AgentEvent 与 ChatEvent 保持不同生命周期并显式投影。

验收：Agent 在 turn 之间无可变领域状态且不依赖 UI、具体 Provider 或 JSONL；流式 turn/compact/resume 互斥；TUI/CLI 不越过 Chat 访问内部模块。

## 7. 收尾

- [ ] 将 `core` 中剩余设置迁入 `config`，组合逻辑迁入 `bootstrap.py`。
- [ ] 删除所有临时 import 兼容层和 AST 例外。
- [ ] 删除未使用的 Protocol、DTO 和空包。
- [ ] 更新现有架构文档，删除或标记被替代的包边界结论。
- [ ] 运行完整验证命令。
- [ ] 检查生产模块依赖图无环。
- [ ] 对照状态表逐项验证创建、更新、恢复、切换、取消和销毁路径。

验收：AST 允许表不含临时例外，所有检查通过，模块公开 API 与 `02-modules.md` 一致。
