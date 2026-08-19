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
| `model` | provider-neutral 请求、响应、事件、usage 和能力 |
| `conversation` | canonical entry/value 类型与事实不变量 |
| `application` | AppState、活动 Session、ProviderRuntime 与 runtime operation lock |
| `workspace` | 工作区路径与文件系统安全原语 |
| `permissions` | 规则、决策、确认和 policy |
| `prompts` | System prompt sections 与 registry |
| `auth` | 凭据存储和解析 |
| `config` | 路径、settings、provider profile 配置 |
| `context` | Conversation 到 ModelRequest 的投影、预算与 compact proposal |
| `tools` | Tool、registry、权限执行和 ToolRound |
| `sessions` | canonical conversation/working set、session context state、私有 JSONL、恢复和提交 |
| `providers` | SDK adapter、发现、缓存和 runtime router |
| `agent` | 无状态 turn 状态机和终态 |
| `features.*` | 文件提及、Todo 等纵向能力 |
| `chat` | 基于 AppState 的用户级用例与安全 view |
| `cli` / `tui` | host 输入输出 |
| `bootstrap` | 唯一对象组装入口 |

## 依赖顺序

```text
model / conversation / workspace
        ↓
permissions / prompts / auth / config
        ↓
context / tools / sessions / providers
        ↓
application / agent / features
        ↓
chat
        ↓
cli / tui
        ↓
bootstrap
```

精确允许表以 `tests/architecture/dependency_rules.py` 为准。Feature 按 `features.<name>` 单独识别，不能互相形成隐式共享层。

## 状态边界

- AppState 是 workspace、active Session、runtime permissions 和 ProviderRuntime 的唯一入口，不是全局 service locator。
- Session 是 canonical conversation、working set、非持久化 delivery/cache、工具结果绑定与 replay sidecar 的唯一所有者。
- AgentEngine、ContextEngine、ToolExecutor 和 Provider adapter 不持有 AppState 或第二份 conversation。
- ChatService 只协调 AppState operation，不复制其状态字段。
- ProviderRuntime 原子切换 connection/client/capabilities/environment，并关闭旧 client。
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
