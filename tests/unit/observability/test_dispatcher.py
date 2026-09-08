"""固定订阅 dispatcher 的投递、上下文与故障语义。"""

from my_code.observability.api import NoOpTracer, RunObservationContext
from my_code.observability.dispatcher import ObservationDispatcher, ObservationRecord
from my_code.observability.events import InvocationStarted


class _CollectingSink:
    def __init__(self) -> None:
        self.records: list[ObservationRecord] = []
        self.shutdown_calls = 0

    def consume(self, record: ObservationRecord) -> None:
        self.records.append(record)

    def shutdown(self, timeout_millis: int) -> None:
        assert timeout_millis >= 0
        self.shutdown_calls += 1


class _FailingSink(_CollectingSink):
    def consume(self, record: ObservationRecord) -> None:
        del record
        raise OSError("private sink failure")


def test_publish_freezes_one_record_for_all_sinks_and_isolates_failure() -> None:
    first = _CollectingSink()
    second = _CollectingSink()
    observations = ObservationDispatcher(NoOpTracer(), (first, _FailingSink(), second))
    context = RunObservationContext("session", "run", "invocation", "main")

    with observations.bind_run(context):
        observations.publish(InvocationStarted(False))

    assert first.records[0] is second.records[0]
    assert first.records[0].context == context
    assert first.records[0].sequence == 0
    assert first.records[0].event_id


def test_shutdown_is_idempotent_and_ignores_later_events() -> None:
    sink = _CollectingSink()
    observations = ObservationDispatcher(NoOpTracer(), (sink,))

    observations.shutdown()
    observations.shutdown()
    observations.publish(InvocationStarted(False))

    assert sink.shutdown_calls == 1
    assert sink.records == []
