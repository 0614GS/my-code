"""Content-addressed JSONL storage for provider-neutral model requests."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import fields, is_dataclass, replace
from enum import StrEnum
from pathlib import Path

from my_code.foundation.json import JsonObject, JsonValue, to_json_object
from my_code.model.invocation import (
    ModelInputOrigin,
    ModelInputOriginKind,
    ModelInvocation,
    ModelInvocationStatus,
    RequestPurpose,
)
from my_code.model.request import (
    AssistantOutput,
    InputDocument,
    InputImage,
    InputText,
    ModelReasoningBlock,
    ModelTextBlock,
    ModelToolUseBlock,
    ToolOutputDocument,
    ToolOutputImage,
    ToolOutputs,
    ToolOutputText,
    UserInput,
)
from my_code.sessions.request_audit import (
    RequestAuditManifest,
    RequestAuditSnapshot,
    ResolvedAuditRequest,
)

_SCHEMA_VERSION = 1
_TERMINAL_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "context-overflow", "delivery-unknown"}
)


class RequestAuditStore:
    """Validate once on open, then append request facts persistence-first."""

    def __init__(self, session_dir: Path) -> None:
        self.path = session_dir / "request-audit.jsonl"
        self._legacy_missing = not self.path.exists()
        self._blobs: dict[str, JsonObject] = {}
        self._manifests: list[RequestAuditManifest] = []
        self._manifest_index: dict[str, int] = {}
        self._revision = 0
        self._load()

    def prepare(self, invocation: ModelInvocation) -> RequestAuditManifest:
        if invocation.request_id in self._manifest_index:
            raise ValueError(f"Duplicate request audit ID: {invocation.request_id}")
        prompt_values = tuple(
            to_json_object(
                {
                    "key": section.key,
                    "content": section.content,
                    "stability": section.stability.value,
                }
            )
            for section in invocation.request.system_prompt.sections
        )
        input_values = tuple(_input_value(item) for item in invocation.request.input)
        tool_values = tuple(
            to_json_object(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                }
            )
            for tool in invocation.request.tools
        )
        records: list[JsonObject] = []
        pending_blobs: set[str] = set()
        refs_by_kind: list[tuple[str, ...]] = []
        for kind, values in (
            ("system_prompt_section", prompt_values),
            ("model_input", input_values),
            ("tool_definition", tool_values),
        ):
            refs: list[str] = []
            for value in values:
                digest = _digest(value)
                refs.append(digest)
                previous = self._blobs.get(digest)
                if previous is not None and previous != value:
                    raise ValueError(f"Request audit hash collision: {digest}")
                if previous is None and digest not in pending_blobs:
                    pending_blobs.add(digest)
                    records.append(
                        to_json_object(
                            {
                                "version": _SCHEMA_VERSION,
                                "type": "blob",
                                "kind": kind,
                                "sha256": digest,
                                "value": value,
                            }
                        )
                    )
            refs_by_kind.append(tuple(refs))
        prompt_refs, input_refs, tool_refs = refs_by_kind
        manifest = RequestAuditManifest(
            request_id=invocation.request_id,
            request_number=len(self._manifests) + 1,
            purpose=invocation.purpose,
            causal_head=invocation.causal_head,
            step=invocation.step,
            attempt=invocation.attempt,
            compact_trigger=invocation.compact_trigger,
            system_prompt_refs=prompt_refs,
            input_refs=input_refs,
            tool_refs=tool_refs,
            origins=invocation.origins,
            max_output_tokens=invocation.request.max_output_tokens,
            reasoning_mode=invocation.request.reasoning_mode,
            budget=(
                to_json_object(_jsonable(invocation.budget))
                if invocation.budget is not None
                else None
            ),
        )
        records.append(_manifest_record(manifest))
        self._append(records)
        for record in records:
            if record.get("type") == "blob":
                self._blobs[str(record["sha256"])] = to_json_object(record["value"])
        self._manifest_index[manifest.request_id] = len(self._manifests)
        self._manifests.append(manifest)
        self._legacy_missing = False
        return manifest

    def finish(
        self,
        request_id: str,
        status: ModelInvocationStatus,
        error: str | None = None,
    ) -> None:
        if status not in _TERMINAL_STATUSES:
            raise ValueError(f"Invalid terminal request status: {status}")
        try:
            index = self._manifest_index[request_id]
        except KeyError as exc:
            raise ValueError(f"Unknown request audit ID: {request_id}") from exc
        current = self._manifests[index]
        if current.status != "prepared":
            raise ValueError(f"Request audit is already terminal: {request_id}")
        record = to_json_object(
            {
                "version": _SCHEMA_VERSION,
                "type": "status",
                "request_id": request_id,
                "status": status,
                "error": error,
            }
        )
        self._append((record,))
        self._manifests[index] = replace(current, status=status, error=error)

    def snapshot(self) -> RequestAuditSnapshot:
        resolved = tuple(
            ResolvedAuditRequest(
                manifest,
                tuple(self._blobs[ref] for ref in manifest.system_prompt_refs),
                tuple(self._blobs[ref] for ref in manifest.input_refs),
                tuple(self._blobs[ref] for ref in manifest.tool_refs),
            )
            for manifest in self._manifests
        )
        return RequestAuditSnapshot(
            self._legacy_missing,
            self._revision,
            resolved,
        )

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            contents = self.path.read_bytes()
            lines = contents.decode("utf-8").splitlines()
        except (OSError, UnicodeError) as error:
            raise ValueError(
                f"Cannot read request audit: {self.path}: {error}"
            ) from error
        for line_number, line in enumerate(lines, 1):
            if not line.strip():
                raise ValueError(f"Invalid blank request audit line {line_number}")
            try:
                raw = json.loads(line)
                if not isinstance(raw, dict) or raw.get("version") != _SCHEMA_VERSION:
                    raise ValueError("unsupported record")
                kind = raw.get("type")
                if kind == "blob":
                    self._load_blob(raw)
                elif kind == "manifest":
                    self._load_manifest(raw)
                elif kind == "status":
                    self._load_status(raw)
                else:
                    raise ValueError(f"unknown record type {kind!r}")
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(
                    f"Invalid request audit line {line_number}: {error}"
                ) from error
        self._manifests = [
            replace(manifest, status="delivery-unknown")
            if manifest.status == "prepared"
            else manifest
            for manifest in self._manifests
        ]
        digest = hashlib.sha256(contents).digest()
        self._revision = int.from_bytes(digest[:8], "big")

    def _load_blob(self, raw: dict[str, object]) -> None:
        digest = _required_str(raw, "sha256")
        value = to_json_object(raw["value"])
        if _digest(value) != digest:
            raise ValueError(f"blob hash mismatch: {digest}")
        previous = self._blobs.get(digest)
        if previous is not None:
            if previous != value:
                raise ValueError(f"blob hash collision: {digest}")
            raise ValueError(f"duplicate blob: {digest}")
        self._blobs[digest] = value

    def _load_manifest(self, raw: dict[str, object]) -> None:
        number = _required_int(raw, "request_number")
        if number != len(self._manifests) + 1:
            raise ValueError("request manifest sequence is not contiguous")
        request_id = _required_str(raw, "request_id")
        if request_id in self._manifest_index:
            raise ValueError(f"duplicate request ID: {request_id}")
        prompt_refs = _refs(raw, "system_prompt_refs")
        input_refs = _refs(raw, "input_refs")
        tool_refs = _refs(raw, "tool_refs")
        for ref in (*prompt_refs, *input_refs, *tool_refs):
            if ref not in self._blobs:
                raise ValueError(f"dangling blob reference: {ref}")
        origins_raw = raw.get("origins")
        if not isinstance(origins_raw, list):
            raise ValueError("origins must be an array")
        origins = tuple(_origin(item) for item in origins_raw)
        if len(origins) != len(input_refs):
            raise ValueError("origin count does not match input references")
        budget_raw = raw.get("budget")
        budget = None if budget_raw is None else to_json_object(budget_raw)
        manifest = RequestAuditManifest(
            request_id=request_id,
            request_number=number,
            purpose=RequestPurpose(_required_str(raw, "purpose")),
            causal_head=_optional_str(raw, "causal_head"),
            step=_required_int(raw, "step"),
            attempt=_required_int(raw, "attempt"),
            compact_trigger=_optional_str(raw, "compact_trigger"),
            system_prompt_refs=prompt_refs,
            input_refs=input_refs,
            tool_refs=tool_refs,
            origins=origins,
            max_output_tokens=_required_int(raw, "max_output_tokens"),
            reasoning_mode=_required_str(raw, "reasoning_mode"),
            budget=budget,
        )
        self._manifest_index[request_id] = len(self._manifests)
        self._manifests.append(manifest)

    def _load_status(self, raw: dict[str, object]) -> None:
        request_id = _required_str(raw, "request_id")
        if request_id not in self._manifest_index:
            raise ValueError(f"status references unknown request: {request_id}")
        status = _required_str(raw, "status")
        if status not in _TERMINAL_STATUSES:
            raise ValueError(f"invalid request status: {status}")
        index = self._manifest_index[request_id]
        current = self._manifests[index]
        if current.status != "prepared":
            raise ValueError(f"duplicate terminal status: {request_id}")
        self._manifests[index] = replace(
            current,
            status=status,  # type: ignore[arg-type]
            error=_optional_str(raw, "error"),
        )

    def _append(self, records: tuple[JsonObject, ...] | list[JsonObject]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = b"".join(
            json.dumps(
                record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            + b"\n"
            for record in records
        )
        descriptor = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written < 1:
                    raise OSError("request audit append made no progress")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        digest = hashlib.sha256(self._revision.to_bytes(8, "big") + payload).digest()
        self._revision = int.from_bytes(digest[:8], "big")


def _manifest_record(manifest: RequestAuditManifest) -> JsonObject:
    return to_json_object(
        {
            "version": _SCHEMA_VERSION,
            "type": "manifest",
            "request_id": manifest.request_id,
            "request_number": manifest.request_number,
            "purpose": manifest.purpose.value,
            "causal_head": manifest.causal_head,
            "step": manifest.step,
            "attempt": manifest.attempt,
            "compact_trigger": manifest.compact_trigger,
            "system_prompt_refs": list(manifest.system_prompt_refs),
            "input_refs": list(manifest.input_refs),
            "tool_refs": list(manifest.tool_refs),
            "origins": [_jsonable(origin) for origin in manifest.origins],
            "max_output_tokens": manifest.max_output_tokens,
            "reasoning_mode": manifest.reasoning_mode,
            "budget": manifest.budget,
        }
    )


def _digest(value: JsonObject) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _input_value(item: object) -> JsonObject:
    if isinstance(item, UserInput):
        return to_json_object(
            {
                "type": item.type,
                "content": [_input_block(block) for block in item.content],
            }
        )
    if isinstance(item, AssistantOutput):
        return to_json_object(
            {
                "type": item.type,
                "content": [_assistant_block(block) for block in item.content],
            }
        )
    if isinstance(item, ToolOutputs):
        return to_json_object(
            {
                "type": item.type,
                "results": [
                    {
                        "call_id": result.call_id,
                        "is_error": result.is_error,
                        "content": [_output_block(block) for block in result.content],
                    }
                    for result in item.results
                ],
            }
        )
    raise TypeError(f"Unsupported model input item: {type(item).__name__}")


def _input_block(block: object) -> JsonObject:
    if isinstance(block, InputText):
        return to_json_object({"type": block.type, "text": block.text})
    if isinstance(block, InputImage):
        return to_json_object(
            {"type": block.type, "media_type": block.media_type, "data": block.data}
        )
    if isinstance(block, InputDocument):
        return to_json_object(
            {
                "type": block.type,
                "media_type": block.media_type,
                "data": block.data,
                "name": block.name,
            }
        )
    raise TypeError(f"Unsupported input block: {type(block).__name__}")


def _assistant_block(block: object) -> JsonObject:
    # Provider continuation is intentionally excluded: it is opaque wire state.
    if isinstance(block, ModelTextBlock):
        return to_json_object({"type": block.type, "text": block.text})
    if isinstance(block, ModelToolUseBlock):
        return to_json_object(
            {
                "type": block.type,
                "id": block.id,
                "name": block.name,
                "input": block.input,
            }
        )
    if isinstance(block, ModelReasoningBlock):
        presentation = block.presentation
        return to_json_object(
            {
                "type": block.type,
                "id": block.id,
                "disclosure": presentation.disclosure,
                "parts": list(presentation.parts)
                if presentation.disclosure not in {"hidden", "redacted"}
                else [],
            }
        )
    raise TypeError(f"Unsupported assistant block: {type(block).__name__}")


def _output_block(block: object) -> JsonObject:
    if isinstance(block, ToolOutputText):
        return to_json_object({"type": block.type, "text": block.text})
    if isinstance(block, ToolOutputImage):
        return to_json_object(
            {"type": block.type, "media_type": block.media_type, "data": block.data}
        )
    if isinstance(block, ToolOutputDocument):
        return to_json_object(
            {
                "type": block.type,
                "media_type": block.media_type,
                "data": block.data,
                "name": block.name,
            }
        )
    raise TypeError(f"Unsupported tool output block: {type(block).__name__}")


def _jsonable(value: object) -> JsonValue:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, StrEnum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _jsonable(getattr(value, item.name)) for item in fields(value)
        }
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    raise TypeError(f"Unsupported audit JSON value: {type(value).__name__}")


def _origin(raw: object) -> ModelInputOrigin:
    if not isinstance(raw, dict):
        raise ValueError("origin must be an object")
    return ModelInputOrigin(
        ModelInputOriginKind(_required_str(raw, "kind")),
        _optional_str(raw, "source_id"),
        _optional_str(raw, "source"),
        _optional_str(raw, "attachment_kind"),
    )


def _refs(raw: dict[str, object], key: str) -> tuple[str, ...]:
    value = raw.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{key} must be an array of strings")
    return tuple(value)


def _required_str(raw: dict[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _optional_str(raw: dict[str, object], key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string or null")
    return value


def _required_int(raw: dict[str, object], key: str) -> int:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{key} must be a positive integer")
    return value


__all__: list[str] = []
