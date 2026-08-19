# 阶段 2：建立 Model 边界

完成日期：2026-08-19。

## 最终所有权

`my_code.model` 是 provider-neutral 模型语言的唯一所有者：

- 请求、消息、内容块、输出和有序流事件；
- resolved system prompt、token usage、模型 limits/capabilities；
- provider binding 与不透明 continuation envelope；
- 模型边界使用的 JSON 值以及 context overflow 错误；
- 唯一多态接口 `ModelClient.stream()`。

`collect_model_output()` 从流中取得唯一的 `ModelOutputCompleted`。缺少终态或重复终态都会失败，因此不再需要独立 completion port 或完整响应 adapter。

Prompts 仍拥有未解析 section、resolver 和生命周期缓存；Conversation 仍拥有已发生的消息事实。二者只引用 Model 定义的 resolved prompt、usage、reasoning presentation 和 continuation 值，不建立第二份权威类型。

## 删除的旧入口

- `agent/contracts/model.py`
- `agent/ports/model.py`
- Providers 中重复拥有模型边界的 `base.py`、`catalog.py`、`call.py`、`streaming.py`
- Providers 中仅承载共享 ID 校验的 `ids.py`

Anthropic、OpenAI、ProviderRouter、Agent 和 Context 均从 `my_code.model` 模块根导入能力。Providers 不再反向导入 Agent、Conversation 或 Prompts。

## 架构守卫变化

阶段二临时依赖与深层 import 例外已全部删除。仍存在的 Context→Agent 和 Tools→Agent 分别归后续 Context/Agent、Tool 简化阶段处理，不再伪装成 Model 债务。

与阶段 0 相比：

| 项目 | 阶段 0 | 阶段 2 |
| --- | ---: | ---: |
| 生产 Python 文件 | 132 | 132 |
| 跨模块 import 记录 | 256 | 236 |
| 临时违规模块边 | 45 | 37 |
| 临时深层 import 边 | 51 | 44 |
| 循环分量中的模块数 | 13 + 2 | 12 + 2 |

`model` 当前没有任何跨模块出边。Auth 已退出主要循环分量。

## 验收

| 命令 | 结果 |
| --- | --- |
| `uv run ruff format --check .` | 通过 |
| `uv run ruff check .` | 通过 |
| `uv run pyright` | 通过，0 errors、0 warnings |
| `uv run pytest` | 通过，351 tests passed |

ModelClient 收集器覆盖正常、缺少终态和重复终态路径；架构测试验证 Model 包内只有一个 Protocol。
