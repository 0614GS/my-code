# Session Transcript 与 OpenTelemetry

my-code 只有一个可靠的执行事实源：Session transcript。OpenTelemetry 是可采样、
可丢失的运行观测面，用于时间线、错误类型、重试、耗时与指标，不能用于恢复，也不是
Harness 的数据依赖。

## Invocation Journal

Session transcript 在完整对话事实之外保存两种最小辅助记录：

- `invocation_started` 保存 invocation/run/parent run/agent 身份、开始时间、是否 continuation，
  以及可选的 evaluation run、test case 和 attempt 标识。
- `invocation_finished` 保存结束时间、`succeeded | max_steps | failed | cancelled` outcome，
  以及适用于该 outcome 的 step、usage、limit 或异常类型。

Journal 不复制 prompt、模型响应、工具输入或结果；这些内容已经存在于 canonical
conversation 和外置工具结果中。异常只保存类型，不保存异常文本。没有匹配 finish 的
start 表示进程中断或 journal 写入失败，Harness 应将其判定为 incomplete。v6 的
`turn_started/turn_finished` 在读取时映射为 legacy invocation，不改写原文件。

Invocation 覆盖一次完整 Agent loop；interactive stream 可以在安全 step boundary 接受多条
用户 steering，因此不能把这组 terminal record 解释成单个 Conversation Turn。Turn 仍由
每条已接受 `HumanMessage` 的 causal boundary 推导；如果未来需要持久化 Turn metadata，
必须新增独立 record，不能复用 Invocation journal。

Harness 以 transcript、外置工具结果和 `Session.invocation_history` 为权威输入，并在自己的
存储中保存评分。运行评测不要求启动 Collector。

## Runtime 观测边界

Agent、Context、ToolExecutor 和 Session 都不导入 observability。bootstrap 在 runtime
边界组合三个技术无关的 adapter：

```text
InstrumentedAgentRunner
└── invoke_agent <name>
    ├── InstrumentedModelClient(purpose=agent) -> chat <model>
    └── InstrumentedToolExecutor -> execute_tool <tool>
        └── tool.blocked_on_user

InstrumentedModelClient(purpose=compaction) -> chat <model>
```

`observability/` 是唯一允许导入 OpenTelemetry SDK 的生产包。runtime adapter 只依赖
`Observer` Protocol，并负责把领域对象映射为 telemetry 语义。所有 telemetry 和 Invocation
Journal 写入故障都只记录日志/事件，不改变 Agent、模型或工具的成功、失败和取消语义。

## OTLP 配置

未配置端点时 `build_observer()` 返回 NoOp observer，不初始化 exporter，也不发起遥测
网络请求。启用 Collector：

```bash
export OTEL_SERVICE_NAME=my-code
export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318
uv run mycode
```

仓库提供 `docs/examples/otel-collector.yaml` 作为最小示例。实现保留 trace、duration
histogram、OTLP HTTP 批量导出和关闭时的有界 flush。

默认只导出 provider/model、TTFC、finish reason、token usage、权限 outcome、错误类型和
耗时等元数据。只有显式设置 `MY_CODE_OTEL_CAPTURE_CONTENT=1` 才添加内容事件；内容限制
为 16 KiB，并带原始字节数、SHA-256 与截断标记。不要把这一开关作为 Harness 输入机制。

旧版本生成的本地 trajectory 文件不会自动删除，但新版本不再写入或读取它们，也不再
提供 trajectory reader、writer 或 composite fan-out。
