# 包边界与架构守卫

my-code 是模块化单体。类型放在哪里由它维护的语义、不变量和生命周期决定，而不是由 import 数量或存储位置决定。

## 模块所有权

| 模块 | 主要所有权 |
| --- | --- |
| `foundation` | 无业务所有者的严格 JSON 基础值 |
| `model` | provider-neutral 请求、输出、事件、usage 和 capability |
| `conversation` | canonical entry/value 类型与事实不变量 |
| `workspace` | 路径、边界和本地文件安全原语 |
| `permissions` | 规则、请求、决策、确认和 policy |
| `prompts` | System prompt section 与 registry |
| `auth` / `config` | 凭据；路径、settings 和 Provider profile |
| `context` | Conversation 到 ModelRequest 的投影、预算与 compact proposal |
| `tools` | Tool、versioned catalog/snapshot、执行和 ToolRound |
| `sessions` | Session 生命周期、私有 JSONL、恢复和语义提交 |
| `providers` | SDK adapter、模型发现和 runtime router |
| `tasks` | 进程内任务状态机、取消树和终态快照 |
| `agent` | 无活动会话状态的 turn/step 循环 |
| `mcp` / `skills` | 各自的发现、runtime 与标准 Tool adapter |
| `runtime` | AppState、ProviderRuntime、ToolState 和 AgentRunFactory |
| `features.*` | File mention、Todo、Subagent、后台任务等纵向能力 |
| `chat` | 基于 AppState 的用户级用例和安全投影 |
| `cli` / `tui` | Host 输入输出 |
| `bootstrap` | 唯一对象组装入口 |

类型所有权和持久化位置不同：`sessions` 可以编码 Conversation fact，但不因此拥有消息语义；`context` 构造 ModelRequest，但不拥有对话历史；Provider 映射 ToolOutput，但不拥有 ToolResult。

## 依赖方向

概念顺序为：

```text
foundation
  -> model / conversation / workspace
  -> permissions / prompts / auth / config
  -> context / tools / sessions / providers / tasks
  -> agent / mcp / skills
  -> runtime / features
  -> chat
  -> cli / tui
  -> bootstrap
```

该图用于阅读，不是授权表。同层不自动互相依赖，Feature 也按 `features.<name>` 分别识别。精确机器规则只维护在 `tests/architecture/dependency_rules.py`，文档不复制一份容易漂移的字典。

## 公开 API

跨模块从语义子模块导入，并由目标模块的静态 `__all__` 声明能力：

```python
from my_code.chat.service import ChatService
from my_code.model.request import ModelRequest
from my_code.permissions.models import PermissionDecision
```

顶层领域包的 `__init__.py` 不聚合 API。生产代码不得跨模块导入以下划线开头的路径、使用 wildcard、引用未声明符号，或通过自己的 `__all__` 转发其他所有者的类型。公开模块在 import 时不得执行注册、读取配置、写文件或发起网络请求。

## 抽象准入

默认依赖具体类；构造函数注入具体对象仍然便于测试。Protocol 只在已经存在多个生产实现或真实调用方扩展点时引入，例如 `ModelClient` 和 permission prompter。

测试 fake、目录层次、形式上的依赖倒置或“未来可能有第二个实现”都不足以创建 Protocol。新抽象必须能说明替换边界、调用者和至少一个真实差异；否则优先直接组合具体能力。

## 状态与安全边界

- `AppState` 只能由 application/host/bootstrap 边界持有，不得通过全局 getter 隐藏依赖。
- Session 是 canonical conversation、派生 context entries、工具结果和 replay 的唯一公开边界。
- Agent、Context、ToolExecutor 和 Provider adapter 不持有 AppState 或第二份 conversation。
- Provider SDK 类型只存在于 `providers`；prompt_toolkit/Rich 只存在于 `tui`。
- Session record、codec 和路径只存在于 `sessions` 私有实现。
- 所有完整对象图和 Tool source 注册都从 `bootstrap.py` 开始。

## 自动架构守卫

`tests/architecture` 使用 Python AST 检查：

- 实际依赖是否符合允许表，允许表与实际图是否无环；
- 跨模块 import 是否来自公开语义模块；
- 私有导入、wildcard、未声明符号和跨所有者 re-export；
- Provider SDK、TUI 库、Session 私有实现、AppState 和 bootstrap 是否泄漏；
- 是否出现 Session 外的可写 conversation 或第二份 runtime permission 权威来源。

架构守卫扫描生产代码；测试可以导入局部实现验证行为。静态守卫不替代权限、取消、恢复、compact 和工具失败路径测试。

主要源码入口：`tests/architecture/dependency_rules.py`、`tests/architecture/test_dependencies.py`、`tests/architecture/test_public_imports.py`。
