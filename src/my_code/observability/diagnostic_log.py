"""有界本地诊断日志；只接受元数据，不保存另一份执行正文。"""

from __future__ import annotations

import json
import logging
import os
from io import TextIOWrapper
from logging.handlers import RotatingFileHandler
from pathlib import Path
from uuid import uuid4

from opentelemetry import trace

from my_code.observability.dispatcher import ObservationRecord
from my_code.observability.events import event_attributes


class _DiagnosticHandler(RotatingFileHandler):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.dropped_events = 0

    def _open(self) -> TextIOWrapper:
        flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.baseFilename, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            return TextIOWrapper(os.fdopen(descriptor, "ab"), encoding="utf-8")
        except BaseException:
            os.close(descriptor)
            raise

    def handleError(self, record: logging.LogRecord) -> None:
        # 不把失败记录或异常正文写到 TUI；下次成功记录会携带累计缺口。
        self.dropped_events += 1


class JsonlObservationSink:
    """每个 application 独享一个轮转文件，不修改全局 logging 配置。"""

    def __init__(self, path: Path, *, max_bytes: int = 5 * 1024 * 1024) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path
        self._handler = _DiagnosticHandler(
            path, maxBytes=max_bytes, backupCount=2, encoding="utf-8"
        )
        self._handler.setFormatter(logging.Formatter("%(message)s"))
        self._closed = False

    def consume(self, record: ObservationRecord) -> None:
        if self._closed:
            return
        try:
            data: dict[str, object] = {
                "version": record.schema_version,
                "event_id": record.event_id,
                "timestamp": record.occurred_at,
                "sequence": record.sequence,
                "event": record.event.name,
                "dropped_events": self._handler.dropped_events,
            }
            if record.context is not None:
                context = record.context
                data.update(
                    session_id=context.session_id,
                    run_id=context.run_id,
                    invocation_id=context.invocation_id,
                    parent_run_id=context.parent_run_id,
                    agent_name=context.agent_name,
                )
            data.update(_bounded_attributes(event_attributes(record.event)))
            span_context = trace.get_current_span().get_span_context()
            if span_context.is_valid:
                data["trace_id"] = format(span_context.trace_id, "032x")
                data["span_id"] = format(span_context.span_id, "016x")
            log_record = logging.LogRecord(
                "my_code.diagnostics",
                logging.INFO,
                "",
                0,
                json.dumps(data, ensure_ascii=True),
                (),
                None,
            )
            self._handler.handle(log_record)
        except Exception:
            # 诊断文件损坏或磁盘已满不能改变工具/模型执行结果。
            self._handler.dropped_events += 1
            return

    def shutdown(self, timeout_millis: int = 0) -> None:
        del timeout_millis
        self._closed = True
        self._handler.close()

    def close(self) -> None:
        self.shutdown()


def build_diagnostic_log(project_state_dir: Path) -> JsonlObservationSink | None:
    """诊断默认本地启用；初始化失败或显式关闭均不妨碍 application 启动。"""

    if os.environ.get("MY_CODE_DIAGNOSTICS", "1").casefold() in {"0", "false", "off"}:
        return None
    try:
        return JsonlObservationSink(
            project_state_dir / "diagnostics" / f"{uuid4()}.jsonl"
        )
    except Exception:
        logging.getLogger(__name__).warning("Local diagnostics unavailable")
        return None


def read_diagnostic_timeline(
    project_state_dir: Path,
    session_id: str,
    *,
    request_id: str | None = None,
) -> dict[str, object]:
    """只读聚合 v1/v2 metadata；损坏行只形成 evidence gap。"""

    directory = project_state_dir / "diagnostics"
    if not directory.is_dir():
        return {
            "records": [],
            "files_scanned": 0,
            "malformed_records": 0,
            "evidence_gap": "diagnostic_directory_missing",
        }
    records: list[dict[str, object]] = []
    malformed = 0
    files = tuple(path for path in sorted(directory.iterdir()) if path.is_file())
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            malformed += 1
            continue
        for line in lines:
            try:
                value = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                malformed += 1
                continue
            if not isinstance(value, dict) or value.get("version") not in {1, 2}:
                malformed += 1
                continue
            if value.get("session_id") != session_id:
                continue
            if request_id is not None and value.get("request_id") != request_id:
                continue
            records.append(
                {
                    str(key): item
                    for key, item in value.items()
                    if isinstance(key, str)
                    and isinstance(item, (type(None), bool, int, float, str))
                }
            )
    records.sort(key=_record_sort_key)
    result: dict[str, object] = {
        "records": records,
        "files_scanned": len(files),
        "malformed_records": malformed,
    }
    if not records:
        result["evidence_gap"] = "no_matching_diagnostic_records"
    elif malformed:
        result["evidence_gap"] = "diagnostic_records_incomplete"
    return result


def _bounded_attributes(
    attributes: dict[str, bool | int | float | str],
) -> dict[str, bool | int | float | str]:
    return {
        key: value[:1024] if isinstance(value, str) else value
        for key, value in attributes.items()
    }


def _record_sort_key(record: dict[str, object]) -> tuple[str, int]:
    sequence = record.get("sequence")
    return (
        str(record.get("timestamp", "")),
        sequence if isinstance(sequence, int) else 0,
    )


__all__ = [
    "JsonlObservationSink",
    "build_diagnostic_log",
    "read_diagnostic_timeline",
]
