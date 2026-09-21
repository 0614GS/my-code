# 执行证据与诊断

my-code 只有一个可靠的执行事实源：Session transcript。OpenTelemetry 是可采样、
可丢失的运行观测面，用于时间线、错误类型、重试、耗时与指标，不能用于恢复，也不是
Harness 的数据依赖。

## 职责与实施边界

| 数据 | 所有者 | 用途与故障语义 |
| --- | --- | --- |
| conversation、request audit | Session | 正文证据与实际语义请求；沿用持久化校验与 audit-before-delivery |
| invocation journal | Session | 最小生命周期辅助记录；写入失败不覆盖业务结果，缺口显式可见 |
| 类型化 observation event | runtime 发布、observability 分发 | 连接诊断 sink，不是业务命令或持久化事实 |
| 本地 metadata JSONL | observability subscriber | 默认开启、可丢失、可轮转的排障时间线，不用于恢复 |
| logs、span、metrics | OTel | 可选运行遥测，不作为评测账本或正文存储 |
| badcase report | sessions 只读派生 | 聚合原始证据中的线索，不反向改写会话、不自动干预 Agent |

`ObservationDispatcher` 是进程内、同步、固定订阅的窄事件总线。它在发布时冻结 event ID、
时间、sequence 与 run context，再分别投递本地 JSONL 和 OTel sink。单个 subscriber 失败不
影响其他 subscriber 或业务结果。它不提供全局 registry、动态订阅或业务消息投递。

## 本地诊断日志

每次 application 在 `<project_state_dir>/diagnostics/<application-uuid>.jsonl`
写入元数据日志。每个文件最多 5 MiB，保留两份轮转备份；不同 application 使用不同文件，
避免多进程同时轮转。历史 application 文件不自动删除，由用户按保留策略清理。
文件权限为 0600，新建目录为 0700。`MY_CODE_DIAGNOSTICS=0` 关闭本地日志，
与 OTel 开关互不依赖。关闭 application 时关闭文件，不修改全局 logging handler。

v2 日志包含 event ID、时间、序号、session/run/invocation、request ID、step/attempt/purpose、
provider/model、工具 call ID、参数指纹、错误标志、终态与耗时。模型响应另含普通输入、
输出、缓存读写 token、provider_reported 与 stop reason。字段使用白名单，字符串限长；
不保存 prompt、工具参数正文、错误提示正文、异常 message/stacktrace 或权限 reason detail。
工具名及模型名仍可能是私有元数据，日志不应未经审查上传。

`model.response.received` 表示收到完成输出，`model.request.finished` 表示底层流自然结束，
并不保证
上层协议校验或持久化成功；request audit 的终态才反映 coordinator 是否接受该请求。
首内部事件 `first_event_ms` 与首可见文本 `first_text_ms` 分开；纯工具响应可能没有后者。
token 只按每个 request 的 `model.response.received` 计一次，不能再把 terminal/invocation
指标相加。
未收到 usage 的失败请求费用未知，不填零。my-code 发起的 SSE 重放会使用独立 request
manifest，并将中断 attempt 记录为 `delivery-unknown`；SDK 在响应头前执行的内部网络重试
仍不逐次记录。

本地日志故障不能覆盖业务异常，后续成功记录中的 `dropped_events` 表示累计写入缺口。
进程被强杀可能没有 terminal；日志轮转也可能移除 start，这两者都不等同于已确认失败。
日志不 fsync，不提供审计级持久性承诺。原始正文仍通过 Session/request audit 查找。

`ModelRequest.identity` 只在 coordinator 调用边界携带已有审计 ID、step、attempt、purpose，
不参与 request equality、provider payload 或 prompt cache key；不另造一个请求 ID。

## Badcase 排查

先停止会话写入，或准备一致性副本，再运行：

```bash
uv run python scripts/analyze_session.py <project_state_dir> <session_id>
uv run python scripts/analyze_session.py <project_state_dir> <session_id> --include-content
uv run python scripts/analyze_session.py <project_state_dir> <session_id> --request-id <request_id>
```

`project_state_dir` 是保存 `<session_id>.jsonl` 的目录，默认位于
`~/.my-code/projects/<规范化工作区路径>/`。报告写到 stdout，不创建或修复 Session 文件。
`inspect_session()` 使用 Session 所有者内部的 reader，不调用会自动闭合工具的恢复接口。
缺失/损坏的 canonical 证据明确失败；旧会话无 request audit 时报告历史缺口。报告还会
扫描 `diagnostics/` 中兼容的 v1/v2 文件并附加 metadata timeline；轮转、损坏或没有匹配
记录时只标记 evidence gap，不改变 canonical badcase 结论。

默认报告不含正文，列出工具错误、未闭合调用、异常/取消/步数上限/incomplete、请求终态，
以及同一用户 turn 内连续重复至少三次的工具轮次（模式长度 1~4）。重复是待核查信号，
不是死循环判定；正常轮询也可能重复，非完全相同参数的循环可能漏检。
工具错误携带 result entry ID 及确实引用该来源的后续 request ID。
`--include-content` 输出模型可见工具错误与输入；`--request-id` 显式输出指定请求的
system/input/tools/budget。两者可能包含敏感正文，只用于受控本地排障。

建议按“失败/重复线索 -> 工具调用及错误提示 -> 后续实际请求 -> harness 假设 ->
单变量修复与回归测试”分析。不要把 max_steps、正常取消或工具 is_error 自动当成根因。
报告中的用量只覆盖已提交 assistant，不包括所有压缩、失败或 SDK 重试费用。
大型工具结果可能只有 preview，外置临时文件可能已过期，报告不假装能还原缺失内容。

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

## 无头执行协议

`mycode run [PROMPT]` 执行一个非交互回合；省略位置参数时从 stdin 读取。它不会启动
Provider 向导、权限确认或 Question UI。默认权限模式固定为 `dontAsk`，与项目默认值无关；
`--dangerously-skip-permissions` 才启用 bypass，并且每次进程都要重新声明。bypass 自动批准
普通 ASK，但显式 deny、工作区边界、受保护路径和 bypass-immune 检查仍生效。

`--output-format text` 只在 stdout 写最终回答；`json` 写一个 v1 result；`stream-json`
逐行写 system、前端事件和唯一的 terminal result。机器协议包含 session/run/invocation ID、
普通输入、缓存创建、缓存读取、输出 token、实际 sandbox 状态，以及 session、request audit
和 diagnostics 的路径。诊断信息写 stderr，不污染 stdout。退出码分别为成功 0、运行异常 1、
参数/配置错误 2、max steps 3、超时 124 和中断 130。

`--sandbox-mode auto|local` 只选择 my-code 的 Bash launcher。无头 CLI 不探测 Docker 是否
安全，也不验证外部网络隔离；选择 local 或危险绕过时，调用方负责容器、网络、仓库准备和
资源限制。数据集解析、任务注入与评分应由独立 adapter 完成，不进入产品 CLI。

评测 harness 可以同时传入 `--evaluation-run-id`、`--test-case-id` 和 `--attempt-id`。
非空字段会组成 `EvaluationContext`，写入根运行和子 Agent 的 invocation journal、诊断
上下文，以及 `system`/`result` 机器记录的可空 `evaluation` 对象。schema version 仍为 1；
未传入时该字段为 `null`。

`--ignore-project-settings` 只跳过工作区 `.my-code/settings.json`、
`settings.local.json` 和项目 Skill 搜索根。用户级 provider、凭据、设置、MCP/Skill 配置仍
有效，`AGENTS.md` 与普通工作区文件仍进入正常上下文。该开关用于不可信评测仓库，不能替代
容器隔离。

## Harbor v0.23.0 adapter

`integrations/harbor/agent.py:MyCodeAgent` 是仓库外层 adapter；核心包不导入 Harbor。
先生成固定安装产物：

```bash
uv run python scripts/build_harbor_artifacts.py /tmp/mycode-artifacts
```

目录包含 wheel、由 `uv.lock` 导出的精确 `constraints.txt`、当前 uv 二进制，以及带
wheel/constraints/uv/lock SHA-256 和 `.python-version` selector 的 `manifest.json`。adapter
把 uv 一并上传，并在每个 task 容器内创建 uv-managed Python 3.12 和独立 venv；不依赖任务
镜像的系统 Python，也不复制宿主机 `.venv`。Harbor agent kwarg `artifact_dir` 必填。模型连接完全由
`MYCODE_MODEL`、`MYCODE_PROTOCOL`、`MYCODE_API_KEY` 和可选的 `MYCODE_BASE_URL` 定义；协议必须
显式选择 `anthropic-messages` 或 `openai-responses`。adapter 不使用 Harbor 的 provider 推断，
也不经由 LiteLLM 请求模型。它在容器中创建独立 venv，通过 my-code 配置 store API 写临时
provider，并以 stdin 运行
`mycode run --output-format stream-json --ignore-project-settings`。API key 只存在于该临时
进程环境和随后删除的私有配置中。

仓库提供 SWE-bench Verified 启动脚本。它默认运行一个 smoke task；先复制示例配置并填写
模型凭据：

```bash
cp .env.harbor.example .env.harbor
scripts/run_harbor_swebench.sh
```

并发、attempt 数、模型、协议、数据集和 artifact 目录都可以在 `.env.harbor` 中调整；额外的
Harbor 参数可以直接追加到脚本命令后。`.env.harbor` 被 gitignore，示例文件不得包含真实凭据。

Harbor agent 日志包含 `stream.jsonl`、`stderr.log`、`result.json`、`mycode/projects/` 原生
证据和转换成功时的 ATIF-v1.7 `trajectory.json`。`populate_context_post_run()` 只解析 terminal
result 填充 token/cache 与 metadata；非零退出按机器字段分类，不扫描模型正文。ATIF 转换
失败只记录 `trajectory_conversion_error`，不改写原始结果。

job 级只读汇总：

```bash
uv run python scripts/analyze_harbor_job.py <harbor-job-dir>
uv run python scripts/analyze_harbor_job.py <harbor-job-dir> --format json
uv run python scripts/analyze_harbor_job.py <harbor-job-dir> --format csv
```

默认命令将完整 JSON 及 trial、tool、incident CSV 写入 `<harbor-job-dir>/analysis/`，
stdout 只显示摘要；显式 `--format` 保留只向 stdout 输出的兼容模式。分析器联合 Harbor
reward/exception、my-code terminal result、canonical Session、request audit 和本地 diagnostics，
报告 cache 命中、模型请求、工具成功/拒绝/超时/非零退出、耗时、重复调用与证据缺口。
canonical Session 是首选事实源，ATIF 只在原生 Session 缺失时降级使用；两种来源不会重复计数。
默认报告不包含工具参数或结果正文，`--include-content` 仅用于受控本地排障。分析器不会启动
Collector，也不会修改 trial、Session 或 verifier 证据。

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
`ObservationDispatcher`，并负责把领域对象映射为类型化 metadata event。Agent、Model、
Tool、Session 不发布 observation event。所有 telemetry 和 Invocation
Journal 写入故障都只记录日志/事件，不改变 Agent、模型或工具的成功、失败和取消语义。

point event 通过 subscriber fan-out；span 生命周期通过 dispatcher 的 `operation()` scope
直接委托 OTel `start_as_current_span`。OTel Context 负责 asyncio task 内的父子传播，不通过
started/finished 事件维护 span 状态表，也不自建 trace ID 或传播协议。

## OTLP 配置

未配置端点时使用 NoOp tracer，不初始化 exporter，也不发起遥测网络请求；本地 JSONL
仍独立工作。启用 Collector：

```bash
export OTEL_SERVICE_NAME=my-code
export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318
uv run mycode
```

通用 endpoint 启用 logs、traces、metrics；也可以只配置标准的
`OTEL_EXPORTER_OTLP_LOGS_ENDPOINT`、`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` 或
`OTEL_EXPORTER_OTLP_METRICS_ENDPOINT`，未配置的 signal 不创建 exporter。

仓库提供 `docs/examples/otel-collector.yaml` 作为最小示例。实现导出 trace、OTel Logs、
事件计数、Agent/Tool/Model 耗时和四类 token usage。metric attributes 不包含 request、
invocation、tool call 等高基数 ID。三个 provider 共享关闭总预算并使用批量 OTLP HTTP 导出。

默认只导出 provider/model、TTFC、finish reason、token usage、权限 outcome、错误类型和
耗时等元数据。只有显式设置 `MY_CODE_OTEL_CAPTURE_CONTENT=1` 才添加内容事件；内容限制
为 16 KiB，并带原始字节数、SHA-256 与截断标记。不要把这一开关作为 Harness 输入机制。
异常 message/stacktrace 即使开启内容捕获也不自动导出。span 写操作与清理失败均在
runtime 边界隔离，关闭 exporter 共用总时间预算；超时后放弃等待，不保证遥测送达。

旧版本生成的本地 trajectory 文件不会自动删除，但新版本不再写入或读取它们，也不再
提供 trajectory reader、writer 或 composite fan-out。
