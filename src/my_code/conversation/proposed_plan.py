"""Streaming parser for exclusive-line ``<proposed_plan>`` blocks."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

OPEN_TAG = "<proposed_plan>"
CLOSE_TAG = "</proposed_plan>"


class PlanSegmentKind(StrEnum):
    TEXT = "text"
    START = "start"
    DELTA = "delta"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class PlanSegment:
    kind: PlanSegmentKind
    text: str = ""


class ProposedPlanParser:
    """Recognize tags split across chunks without leaking plan text as normal text."""

    def __init__(self) -> None:
        self._pending = ""
        self._in_plan = False

    def feed(self, chunk: str) -> tuple[PlanSegment, ...]:
        self._pending += chunk
        result: list[PlanSegment] = []
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            result.extend(self._line(line + "\n"))
        return tuple(result)

    def finish(self) -> tuple[PlanSegment, ...]:
        result: list[PlanSegment] = []
        if self._pending:
            result.extend(self._line(self._pending))
            self._pending = ""
        if self._in_plan:
            self._in_plan = False
            result.append(PlanSegment(PlanSegmentKind.COMPLETED))
        return tuple(result)

    def _line(self, line: str) -> tuple[PlanSegment, ...]:
        marker = line.rstrip("\r\n").strip()
        if not self._in_plan and marker == OPEN_TAG:
            self._in_plan = True
            return (PlanSegment(PlanSegmentKind.START),)
        if self._in_plan and marker == CLOSE_TAG:
            self._in_plan = False
            return (PlanSegment(PlanSegmentKind.COMPLETED),)
        kind = PlanSegmentKind.DELTA if self._in_plan else PlanSegmentKind.TEXT
        return (PlanSegment(kind, line),)


def strip_proposed_plan(text: str) -> str:
    return _parse_complete(text)[0]


def extract_proposed_plan(text: str) -> str | None:
    return _parse_complete(text)[1]


def _parse_complete(text: str) -> tuple[str, str | None]:
    parser = ProposedPlanParser()
    segments = (*parser.feed(text), *parser.finish())
    visible = "".join(
        item.text for item in segments if item.kind is PlanSegmentKind.TEXT
    )
    plan_parts: list[str] = []
    saw = False
    current: list[str] = []
    for item in segments:
        if item.kind is PlanSegmentKind.START:
            saw = True
            current = []
        elif item.kind is PlanSegmentKind.DELTA:
            current.append(item.text)
        elif item.kind is PlanSegmentKind.COMPLETED:
            plan_parts = current
    return visible, "".join(plan_parts).strip() if saw else None


__all__ = [
    "CLOSE_TAG",
    "OPEN_TAG",
    "PlanSegment",
    "PlanSegmentKind",
    "ProposedPlanParser",
    "extract_proposed_plan",
    "strip_proposed_plan",
]
