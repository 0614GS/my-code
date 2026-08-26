# 模块边界与依赖方向

## API 约定

跨模块代码从语义子模块导入，例如：

```python
from my_code.chat.service import ChatService
from my_code.model.request import ModelRequest
from my_code.permissions.models import PermissionDecision
```

顶层领域包的 `__init__.py` 不聚合 API。每个公开语义模块用静态 `__all__` 声明自己的能力；以下划线开头的模块或路径段视为私有。公开模块不能通过 `__all__` 转发其他所有者的符号。

## 所有权

| 模块 | 拥有的能力 |
| --- | --- |
| `foundation` | 无业务所有者的 JSON 值类型与严格规范化原语；不依赖任何项目内模块 |
| `model` | provider-neutral 请求、响应、事件、usage 和能力 |
| `conversation` | canonical entry/value 类型与事实不变量 |
| `runtime` | AppState、活动 Session、ProviderRuntime、ToolState、AgentRunFactory 与 runtime operation lock |
| `workspace` | 工作区路径与文件系统安全原语 |
| `permissions` | 规则、决策、确认和 policy |
| `prompts` | System prompt sections 与 registry |
| `auth` | 凭据存储和解析 |
| `config` | 路径、settings、provider profile 配置 |
| `context` | Conversation 到 ModelRequest 的投影、预算与 compact proposal |
| `tools` | Tool、versioned catalog/snapshot、权限执行和 ToolRound |
| `sessions` | canonical conversation/context entries、session context state、私有 JSONL、恢复和提交 |
| `providers` | SDK adapter、发现、缓存和 runtime router |
| `tasks` | 进程内任务状态机、父子取消树、终态快照和 runtime events |
| `mcp` | server/transport 生命周期、静态发现、schema 校验、诊断和标准 Tool adapter |
| `skills` | 分层发现、严格 frontmatter、统一 catalog、lazy load、激活状态和标准 Tool adapter |
| `agent` | 无状态 turn 状态机、终态和 provider-neutral run token budget |
| `features.*` | 文件提及、Todo、Subagent 等纵向能力 |
| `chat` | 基于 AppState 的用户级用例与安全 view |
| `cli` / `tui` | host 输入输出 |
| `bootstrap` | 唯一对象组装入口 |

## 依赖顺序

```text
foundation
        ↓
model / conversation / workspace
        ↓
permissions / prompts / auth / config
        ↓
context / tools / sessions / providers / tasks
        ↓
agent / mcp / skills
        ↓
runtime / features
        ↓
chat
        ↓
cli / tui
        ↓
bootstrap
```

精确允许表以 `tests/architecture/dependency_rules.py` 为准。Feature 按 `features.<name>` 单独识别，不能互相形成隐式共享层。

`foundation` 不是通用 `utils` 容器。只有跨多个领域、没有自然业务所有者且不引入项目内依赖的稳定基础值可以进入；领域 DTO 即使被频繁引用，仍保留在其语义所有者中。

## 状态边界

- AppState 是 workspace、active Session、runtime permissions 和 ProviderRuntime 的唯一入口，不是全局 service locator。
- ToolState 持有唯一 application-lifetime ToolCatalog；每个 Agent step 只消费不可变快照。
- TaskSupervisor 持有唯一进程内 task registry；`tasks` 不依赖 Agent、Session、Provider 或 Feature。
- AgentRunFactory 创建独立 Session/Agent/provider lease 胶囊，并由 AppState 统一关闭。
- McpRuntime 持有唯一 MCP connection registry；每个 server 只通过独立 ToolCatalog source 发布工具，wire 类型不离开 `mcp`。
- SkillRuntime/SkillCatalog 持有唯一 Skill index 与诊断；文件和规范化 MCP 数据来源使用同一模型，Skill 目录永远不作为 Python/plugin 导入。
- `features.subagents` 只组合 AgentRunFactory、TaskSupervisor、固定角色 prompt、child-local ToolCatalog 和复制后只能收窄的 PermissionPolicy，不构造 provider adapter。Explore 的通用只读代理仍委托具体 Tool 的 `is_read_only()`，不会复制 Bash 语义。
- Session 是 canonical conversation（含 AttachmentMessage）、派生 context entries、工具结果与 replay sidecar 的唯一所有者；prompt/user-context cache 属于配对的 ContextRuntime。
- AgentEngine、ContextEngine、ToolExecutor 和 Provider adapter 不持有 AppState 或第二份 conversation。
- ChatService 只协调 AppState operation，不复制其状态字段。
- ProviderRuntime 原子切换前台 connection/client/capabilities/environment 和新 lease 的连接；已创建 lease 保持原 binding。
- AppState 按 TaskSupervisor → AgentRunFactory → SkillRuntime → McpRuntime → ProviderRuntime 的顺序关闭，且任一步失败不会跳过后续资源回收。
- PermissionState 持有唯一 runtime PermissionPolicy；policy 不负责磁盘写入。
- request、turn、step、tool round、stream delta 与 pending approval 都是短生命周期状态。

## 架构守卫

AST 测试检查：

- 模块依赖是否在允许表中。
- 目标依赖图和实际依赖图是否无环。
- 跨模块导入是否来自声明了 `__all__` 的公开语义模块。
- 是否导入私有模块、wildcard 或未声明符号。
- 是否跨所有者 re-export。
- Provider SDK、Textual、Session 私有 JSONL 实现、AppState 和 bootstrap 是否泄漏。
- 是否存在 Session 之外的可写 conversation collection 或第二份 permission 权威来源。

测试可以导入实现细节以验证局部行为，但生产依赖必须满足上述规则。

## 抽象原则

默认直接依赖具体类。只有多个生产实现或真实调用方扩展点才引入 Protocol，例如 ModelClient 和 PermissionPrompter。不会为测试 fake、目录层次或未来可能性创建 port/adapter。
