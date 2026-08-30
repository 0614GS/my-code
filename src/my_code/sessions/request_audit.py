"""Public immutable snapshots for semantic model-request auditing."""

from dataclasses import dataclass
from typing import Literal

from my_code.foundation.json import JsonObject
from my_code.model.invocation import (
    ModelInputOrigin,
    ModelInvocationStatus,
    RequestPurpose,
)

type RequestAuditStatus = Literal["prepared"] | ModelInvocationStatus


@dataclass(frozen=True, slots=True)
class RequestAuditManifest:
    request_id: str
    request_number: int
    purpose: RequestPurpose
    causal_head: str | None
    step: int
    attempt: int
    compact_trigger: str | None
    system_prompt_refs: tuple[str, ...]
    input_refs: tuple[str, ...]
    tool_refs: tuple[str, ...]
    origins: tuple[ModelInputOrigin, ...]
    max_output_tokens: int
    reasoning_mode: str
    budget: JsonObject | None
    status: RequestAuditStatus = "prepared"
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ResolvedAuditRequest:
    manifest: RequestAuditManifest
    system_prompt_sections: tuple[JsonObject, ...]
    input: tuple[JsonObject, ...]
    tools: tuple[JsonObject, ...]


@dataclass(frozen=True, slots=True)
class RequestAuditSnapshot:
    legacy_missing: bool
    revision: int
    requests: tuple[ResolvedAuditRequest, ...]


__all__ = [
    "RequestAuditManifest",
    "RequestAuditSnapshot",
    "RequestAuditStatus",
    "ResolvedAuditRequest",
]
