# 模块化重构

本目录定义 nano-code 下一轮重构的目标和执行计划。重构采用模块化单体，目标是让每个概念只有一个明确所有者，并用 AST 测试固定模块依赖。

自阶段 0 起，本目录保存重构决策和阶段记录。`docs/` 顶层文档现已同步为当前实现手册；若历史阶段指标与现状不同，以当前模块文档和 AST 守卫为准。

本轮不追求形式上的 Clean Architecture。具体实现是默认选择；只有存在真实多态需求时才引入 Protocol。

## 文档索引

| 文档 | 内容 |
| --- | --- |
| [01-principles.md](01-principles.md) | 重构目标、非目标和抽象准入规则 |
| [02-modules.md](02-modules.md) | 模块划分及各模块拥有的能力 |
| [03-state-lifecycle.md](03-state-lifecycle.md) | 消息、会话、上下文状态的所有权与生命周期 |
| [04-dependencies.md](04-dependencies.md) | 允许的依赖方向和公开 API 规则 |
| [05-architecture-guard.md](05-architecture-guard.md) | AST 架构守卫的检查范围与实现要求 |
| [06-standards.md](06-standards.md) | 命名、类型检查、测试和完成标准 |
| [07-todo.md](07-todo.md) | 分阶段迁移计划 |
| [08-state-test-checklist.md](08-state-test-checklist.md) | 恢复、切换与状态失效测试清单 |
| [09-phase-0-baseline.md](09-phase-0-baseline.md) | 阶段 0 检查结果和架构债务基线 |
| [10-phase-1-pyright.md](10-phase-1-pyright.md) | 阶段 1 类型检查迁移结果 |
| [11-phase-2-model.md](11-phase-2-model.md) | 阶段 2 Model 边界与依赖收缩结果 |
| [12-phase-3-conversation-session.md](12-phase-3-conversation-session.md) | 阶段 3 Conversation、Session 与状态生命周期结果 |
| [13-phase-4-tools-permissions-workspace.md](13-phase-4-tools-permissions-workspace.md) | 阶段 4 Tool、Permission 与 Workspace 边界结果 |
| [14-phase-5-context-features.md](14-phase-5-context-features.md) | 阶段 5 Context 生命周期与 Features 所有权结果 |
| [15-phase-6-7-finalization.md](15-phase-6-7-finalization.md) | 阶段 6–7 Agent/Chat 简化、配置收尾与最终架构结果 |
| [16-semantic-api-dataclass-review.md](16-semantic-api-dataclass-review.md) | 语义子模块 API、文档同步与 dataclass 审计 |

## 最终判定标准

重构完成时必须同时满足：

- 顶层模块职责可以用一句话说明；
- 一个领域概念只有一个权威定义；
- 生产代码的模块依赖图无环；
- 跨模块依赖符合允许清单；
- 不再保留仅为分层形式存在的 inbound/outbound port；
- `ruff`、Pyright standard、pytest 和 AST 架构守卫全部通过。
