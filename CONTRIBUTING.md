# 本地开发规范

本文是开发者与 Agent 共用的实现规范。架构事实以 `docs/` 和 `tach.toml` 为准，
这里不重复模块依赖表。

## 开发流程

首次开发运行 `uv sync --group dev`。提交改动前执行：

```bash
uv run python scripts/check.py
```

调试时可以直接运行局部命令或目标测试，但不得声称未实际运行的检查已经通过。

## 代码与命名

- 使用 Python 3.12+、四空格缩进和完整类型标注；跨边界优先传递明确类型，
  不传递无结构的 `dict[str, Any]`。
- 模块、函数和变量使用 `snake_case`，类使用 `PascalCase`，常量使用
  `UPPER_SNAKE_CASE`。
- 函数名表达动作和结果。优先使用 `build_`、`load_`、`persist_`、
  `resolve_`、`validate_`、`parse_`、`render_` 等含义明确的动词；布尔值使用
  `is_`、`has_` 或 `can_`。
- 集合使用复数名，映射名体现 key，例如 `sessions`、`session_by_id`。
- 避免 `utils`、`helpers`、`manager`、`process` 等职责含混的名称。框架入口或
  领域语义明确时可以使用 `run`、`handle`。
- 优先早返回和窄职责。文件接近 1,000 行、函数需要大量注释解释控制流时，
  先重新划分职责。
- 不为测试 fake 或假设中的未来实现引入 Protocol；抽象准入遵循架构文档。

## 注释与 docstring

- 注释与 docstring 使用中文；修改已有英文注释所在代码时同步转换，不做无关的
  全仓翻译。
- 注释说明原因、约束、风险、兼容性或非显然顺序，不逐行复述代码。
- 公共契约、复杂生命周期和有副作用的入口应写 docstring；含义明显的私有小函数
  不强制编写。
- 修改实现时同步更新或删除失效注释。
- 临时事项使用 `TODO(#issue): 原因或完成条件`；不写无上下文的 `TODO`、
  `FIXME` 或 `HACK`。
- `noqa`、`type: ignore` 和 `pragma: no cover` 必须限定具体规则，并在同一行说明
  无法通过正常设计消除的原因。

## 测试

- 单元测试镜像源码：`src/my_code/agent/engine.py` 对应
  `tests/unit/agent/test_engine.py`，嵌套包继续保持相同目录层级。
- 源码根模块使用同名测试目录，例如 `bootstrap.py` 对应
  `tests/unit/bootstrap/`。`tests/unit` 根目录不得直接放置 `test_*.py`。
- 一个测试文件可以按行为拆分，但必须有单一主要所有者。跨越多个领域、验证组件
  协作的流程放入 `tests/integration/`；源码结构与所有权约束放入
  `tests/architecture/`。
- 文件命名为 `test_<subject>.py`，测试命名为 `test_<observable_behavior>()`。
  缺陷修复必须增加回归测试；权限、压缩、恢复、工具执行和并发改动同时覆盖失败
  与取消路径。
- 测试不访问真实网络，不使用真实凭据，不用任意 `sleep()` 等待异步状态。
- 一次性实验放在 `/tmp` 或其他未跟踪目录。可重复、需要长期维护的验证才可进入
  `tests/`；长期性能实验放入 `benchmarks/`。

## Commit

使用聚焦的 Conventional Commits：

```text
<type>(<scope>): <中文摘要>
```

常用 type 为 `feat`、`fix`、`refactor`、`test`、`docs`、`perf`、`build`、
`chore`；scope 使用 `agent`、`context`、`sessions`、`permissions`、`tui` 等稳定
领域名。摘要使用祈使语气、不加句号，一个 commit 只表达一个逻辑变化。回归测试、
对应 Tach 声明及架构文档应与实现放在同一个逻辑 commit 中。
