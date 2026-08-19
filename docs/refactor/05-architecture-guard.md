# AST 架构守卫规范

## 目的

架构规则必须由测试执行，不能只依赖文档和 code review。守卫使用 Python 标准库 `ast`，不引入新的架构框架。

建议位置：

```text
tests/architecture/
├── __init__.py
├── dependency_rules.py
├── test_dependencies.py
└── test_public_imports.py
```

## 模块识别

扫描 `src/my_code/**/*.py` 中的 `import` 和 `from ... import ...`。

- 普通顶层包以第一段识别，例如 `my_code.context.budget` 属于 `context`。
- Feature 以两段识别，例如 `my_code.features.todos.models` 属于 `features.todos`。
- 根 `my_code.bootstrap` 单独识别为组合根；旧 `application`、`core` 与 `constants` 路径已删除，不再特殊映射。
- 相对 import 先解析为绝对模块，再进行判定。
- `TYPE_CHECKING` 内的 import 同样是依赖，不忽略。
- 标准库和第三方包不进入内部模块依赖图，但单独检查 SDK 泄漏规则。

## 必须检查

### 1. 允许依赖

每条跨模块 import 必须存在于 `ALLOWED_DEPENDENCIES`。失败信息至少包含源文件、行号、来源模块和目标模块。

### 2. 循环依赖

根据 AST import 生成模块图并做环检测。失败时输出完整路径，例如：

```text
conversation -> tools -> permissions -> conversation
```

允许表本身和实际代码图都必须无环。

### 3. 公开入口与所有权

跨模块 import 必须引用声明了静态 `__all__` 的语义子模块，并且导入符号必须在清单中。守卫同时拒绝：

- 以下划线开头的私有模块或路径段；
- wildcard import；
- 未在目标 `__all__` 声明的符号；
- 通过 `__all__` re-export 其他架构模块拥有的符号；
- 在顶层领域包 `__init__.py` 聚合 API。

模块内部可以自由组织和引用自己的实现文件。

### 4. 技术泄漏

至少检查以下规则：

- `openai` 和 `anthropic` SDK 只能出现在 `providers`；
- `textual` 只能出现在 `tui`；
- JSONL record 只能由 `sessions` 定义和解析；
- `bootstrap` 不能被任何其他生产模块导入；
- `claude-code` 参考源码不能成为生产 import 或打包输入。

## 迁移期例外

初次启用守卫时可以建立临时例外，但每条例外必须包含：

```python
TemporaryViolation(
    source="conversation",
    target="application",
    owner="phase-2",
    reason="ToolResult still carries legacy presentation",
)
```

要求：

- 例外精确到依赖边，必要时精确到文件；
- 每条例外关联迁移阶段；
- 新代码不得增加例外；
- 阶段完成时删除对应例外；
- 不允许永久 wildcard 例外。

扫描结果必须与例外集合相等，而非仅判断为子集。因此新增违规和已经消失但未清理的例外都会使测试失败。循环债务按强连通分量登记，失败信息同时给出一条完整循环路径。

阶段 7 完成后，依赖、技术泄漏与循环三类临时例外集合均为空。旧的 deep-import 债务检查已由语义 API 声明检查替代。后续边界变化应修改正式允许表和设计文档，不得重新引入无截止阶段的例外。

## 测试边界

架构守卫只扫描生产代码。测试可以导入具体子模块，以便验证实现细节；测试中的 import 不参与生产依赖图。

守卫验证静态结构，不替代行为测试。模块迁移仍必须运行完整 pytest。
