# 模块边界与依赖方向

## API 约定

跨模块代码从语义子模块导入，例如：

```python
from nano_code.chat.service import ChatService
from nano_code.model.request import ModelRequest
from nano_code.permissions.models import PermissionDecision
```

顶层领域包的 `__init__.py` 不聚合 API。每个公开语义模块用静态 `__all__` 声明自己的能力；以下划线开头的模块或路径段视为私有。公开模块不能通过 `__all__` 转发其他所有者的符号。

## 所有权

| 模块 | 拥有的能力 |
| --- | --- |
| `model` | provider-neutral 请求、响应、事件、usage 和能力 |
| `conversation` | canonical message facts 和内存工作集 |
| `workspace` | 工作区路径与文件系统安全原语 |
| `permissions` | 规则、决策、确认和 policy |
| `prompts` | System prompt sections 与 registry |
| `auth` | 凭据存储和解析 |
| `config` | 路径、settings、provider profile 配置 |
| `context` | Conversation 到 ModelRequest 的临时投影 |
| `tools` | Tool、registry、权限执行和 ToolRound |
| `sessions` | JSONL、codec、catalog、恢复和提交 |
| `providers` | SDK adapter、发现、缓存和 runtime router |
| `agent` | 无状态 turn 状态机和终态 |
| `features.*` | 文件提及、Todo 等纵向能力 |
| `chat` | 活动 session bundle 和用户级用例 |
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
agent / features
        ↓
chat
        ↓
cli / tui
        ↓
bootstrap
```

精确允许表以 `tests/architecture/dependency_rules.py` 为准。Feature 按 `features.<name>` 单独识别，不能互相形成隐式共享层。

## 状态边界

- AgentEngine 无 Session 状态。
- ChatService 持有一个不可拆分替换的 session bundle。
- Conversation 保存内存事实；Session 提供持久化提交。
- ContextSession 保存非持久化 delivery 和 cache。
- ProviderRouter 只在模型调用之间切换连接。
- PermissionPolicy 保存当前运行期规则，不负责磁盘写入。

## 架构守卫

AST 测试检查：

- 模块依赖是否在允许表中。
- 目标依赖图和实际依赖图是否无环。
- 跨模块导入是否来自声明了 `__all__` 的公开语义模块。
- 是否导入私有模块、wildcard 或未声明符号。
- 是否跨所有者 re-export。
- Provider SDK、Textual、JSONL records 和 bootstrap 是否泄漏。

测试可以导入实现细节以验证局部行为，但生产依赖必须满足上述规则。

## 抽象原则

默认直接依赖具体类。只有多个生产实现或真实调用方扩展点才引入 Protocol，例如 ModelClient 和 PermissionPrompter。不会为测试 fake、目录层次或未来可能性创建 port/adapter。
