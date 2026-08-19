# 阶段 0 基线

记录日期：2026-08-19。当前扫描范围为 `src/my_code/**/*.py`；测试结果包含工作区在阶段 0 开始时已有的未提交测试修改。

## 改造前检查

| 命令 | 结果 |
| --- | --- |
| `uv run pytest` | 通过，342 tests passed，8.99s |
| `uv run ruff check .` | 通过 |
| `uv run ruff format --check .` | 失败：`docs/refactor/04-dependencies.md` 代码块格式不符合 Ruff |
| 迁移前类型检查 | 通过，132 source files 无错误 |

格式失败来自本轮尚未格式化的设计文档代码块，阶段 0 已修正。类型检查结果仅作为迁移前记录；阶段 1 才统一使用 Pyright standard。

## 架构扫描

AST 守卫位于 `tests/architecture`，当前统计如下：

| 项目 | 数量 |
| --- | ---: |
| 生产 Python 文件 | 132 |
| 跨模块 import 记录 | 256 |
| 不符合目标允许表的模块边 | 45 |
| 跨模块深层 import 边 | 51 |
| 技术泄漏 | 2 |
| 有环强连通分量 | 2 |

主要循环分量包含 13 个模块，示例完整路径是 `agent -> chat -> agent`；另一个循环是 `bootstrap -> cli -> bootstrap`。两项技术泄漏是 CLI 导入旧 composition root，以及 `core.paths` 拥有 JSONL 后缀。

目标允许表、按阶段归属的临时依赖边、深层 import、循环分量和技术泄漏都定义在 `tests/architecture/dependency_rules.py`。扫描集合必须与临时清单完全相等：新增债务和已经消除但未删除的例外都会失败。

## 阶段 0 验收

- `docs/refactor` 已声明为本轮重构的权威设计。
- 生命周期验收项已拆到 [08-state-test-checklist.md](08-state-test-checklist.md)。
- 守卫能够输出文件、行号、依赖边、深层 import、完整循环路径和技术泄漏。
- `claude-code/` 只用于核对 Query/Session/compact 不变量，仍由根 `.gitignore` 排除；阶段 0 未修改或打包该快照。

阶段 0 完成后的验证结果：

| 命令 | 结果 |
| --- | --- |
| `uv run pytest` | 通过，349 tests passed |
| `uv run ruff format --check .` | 通过，200 files already formatted |
| `uv run ruff check .` | 通过 |
| 迁移前生产代码类型检查 | 通过，132 source files 无错误 |
| 迁移前架构测试类型检查 | 通过，4 architecture source files 无错误 |
