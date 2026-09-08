"""本地诊断的隐私、轮转、关闭及故障隔离。"""

import json
from pathlib import Path
from typing import cast

from my_code.observability.api import NoOpTracer, RunObservationContext
from my_code.observability.diagnostic_log import (
    JsonlObservationSink,
    build_diagnostic_log,
    read_diagnostic_timeline,
)
from my_code.observability.dispatcher import ObservationDispatcher
from my_code.observability.events import (
    InvocationStarted,
    ModelRequestStarted,
    SensitiveContent,
)


def test_metadata_log_works_without_otel_and_excludes_content(tmp_path: Path) -> None:
    path = tmp_path / "diagnostic.jsonl"
    log = JsonlObservationSink(path)
    observations = ObservationDispatcher(NoOpTracer(), (log,))
    with observations.bind_run(
        RunObservationContext("session", "run", "invocation", "main")
    ):
        observations.publish(
            ModelRequestStarted(
                "agent",
                "provider",
                "model",
                request_id="request",
                content=SensitiveContent("secret"),
            )
        )
    log.close()
    observations.publish(InvocationStarted(False))
    raw = path.read_text()
    record = json.loads(raw)
    assert record["session_id"] == "session"
    assert record["invocation_id"] == "invocation"
    assert record["request_id"] == "request"
    assert "secret" not in raw
    assert "ignored" not in raw
    assert path.stat().st_mode & 0o777 == 0o600


def test_rotation_is_bounded_and_private(tmp_path: Path) -> None:
    log = JsonlObservationSink(tmp_path / "diagnostic.jsonl", max_bytes=300)
    observations = ObservationDispatcher(NoOpTracer(), (log,))
    for _ in range(20):
        observations.publish(InvocationStarted(False))
    log.close()
    files = list(tmp_path.iterdir())
    assert len(files) == 3
    for path in files:
        assert path.stat().st_mode & 0o777 == 0o600
        for line in path.read_text().splitlines():
            assert json.loads(line)["version"] == 2


def test_logging_failure_does_not_escape_and_next_record_reports_gap(
    tmp_path: Path, monkeypatch
) -> None:
    log = JsonlObservationSink(tmp_path / "diagnostic.jsonl")
    observations = ObservationDispatcher(NoOpTracer(), (log,))
    with monkeypatch.context() as patch:

        def fail(_record):
            raise OSError("secret failure")

        patch.setattr(log._handler, "handle", fail)
        observations.publish(InvocationStarted(False))
    observations.publish(InvocationStarted(False))
    log.close()
    record = json.loads(log.path.read_text())
    assert record["dropped_events"] == 1
    assert record["sequence"] == 1


def test_diagnostics_can_be_disabled_without_creating_files(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("MY_CODE_DIAGNOSTICS", "0")
    assert build_diagnostic_log(tmp_path) is None
    assert list(tmp_path.iterdir()) == []


def test_diagnostics_are_created_by_default(tmp_path: Path) -> None:
    sink = build_diagnostic_log(tmp_path)
    assert sink is not None
    assert sink.path.parent == tmp_path / "diagnostics"
    sink.close()


def test_timeline_reads_v1_and_v2_and_reports_malformed_records(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "diagnostics"
    log = JsonlObservationSink(directory / "current.jsonl")
    observations = ObservationDispatcher(NoOpTracer(), (log,))
    with observations.bind_run(
        RunObservationContext("session", "run", "invocation", "main")
    ):
        observations.publish(InvocationStarted(False))
    log.close()
    (directory / "legacy.jsonl").write_text(
        '{"version":1,"timestamp":"2020-01-01","sequence":0,'
        '"event":"legacy","session_id":"session"}\ninvalid\n',
        encoding="utf-8",
    )

    timeline = read_diagnostic_timeline(tmp_path, "session")

    assert timeline["files_scanned"] == 2
    assert timeline["malformed_records"] == 1
    records = cast(list[dict[str, object]], timeline["records"])
    assert [item["version"] for item in records] == [1, 2]
    assert timeline["evidence_gap"] == "diagnostic_records_incomplete"
