# 重构工程标准

## Python 与命名

- Python 版本为 3.12 或更高。
- 使用四空格缩进。
- 模块与函数使用 `snake_case`，类使用 `PascalCase`，常量使用 `UPPER_SNAKE_CASE`。
- 公开模块 API 和跨模块数据结构必须有完整类型标注。
- 内部局部变量优先依赖类型推断，不为满足形式要求增加冗余标注。
- 单个文件接近 1,000 行前必须重新检查职责划分。

避免 `utils.py`、`common.py` 和宽泛的 `types.py`。允许使用 `models.py`，前提是其中类型属于同一领域且数量较少；类型增长后按语义拆分。

## 模块 API

- 跨模块使用模块根公开 API。
- `__all__` 是模块能力清单，不是把所有内部符号重新导出。
- 删除符号时直接更新调用方；重构期间不建立多层长期兼容别名。
- import 模块不得产生文件写入、网络请求、注册或运行时初始化。

## 状态标准

- 每个可变对象必须在类注释中写明生命周期和所有者。
- 持久化状态必须说明 source of truth、提交顺序和失败后的内存语义。
- cache 必须有明确的 key、失效条件和最大生命周期。
- session、turn、request 状态不得混放在同一个无失效入口的 manager 中。
- 恢复和切换必须先构造完整候选状态，再原子替换活动引用。
- 派生状态优先从 canonical facts 重算，不建立第二份可写权威数据。

消息与各级状态的具体约束见 [03-state-lifecycle.md](03-state-lifecycle.md)。

## 类型检查：Pyright standard

本轮统一使用 Pyright，并采用 `standard` 模式。目标是稳定发现真实类型错误，而不是追求 strict 模式下的零告警技巧。

目标配置：

```toml
[dependency-groups]
dev = [
    "pyright",
    "pytest",
    "pytest-asyncio",
    "ruff",
]

[tool.pyright]
pythonVersion = "3.12"
typeCheckingMode = "standard"
include = ["src", "tests"]
exclude = [".venv", "claude-code"]
```

迁移时删除旧检查器的开发依赖和配置，并将所有开发文档、CI 和验证命令统一为：

```bash
uv run pyright
```

不额外开启一组自定义 strict diagnostics 来变相恢复 strict 模式。确需局部忽略时使用 Pyright 支持的精确错误码，并在同一行说明原因；禁止无范围的文件级忽略。

## 格式与静态检查

标准验证命令：

```bash
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest
```

修改代码时可先运行 `uv run ruff format .`。提交前必须使用上述只读检查形式完成最终验证。

## 测试

- 缺陷修复必须增加回归测试。
- 模块迁移必须保持现有行为测试通过。
- 权限、取消、session 恢复、compact 和工具执行继续覆盖失败路径。
- 架构规则放在 `tests/architecture`，行为单元测试放在 `tests/unit`，跨模块流程放在 `tests/integration`。
- 测试 fake 可以直接实现调用所需的方法；不得仅为 fake 创建生产 Protocol。

## 完成定义

一个迁移阶段只有在以下条件全部满足后才可勾选：

- 新模块所有权已写入模块文档；
- 生产调用方已切换到新公开 API；
- 对应旧入口和临时 re-export 已删除；
- AST 临时例外已减少；
- Ruff、Pyright、pytest 和架构守卫通过；
- 行为或持久化格式若有意变化，已单独记录并测试。
