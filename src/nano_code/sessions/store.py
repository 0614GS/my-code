"""Append-only JSONL transcripts with parent-chain validation."""

import json
import os
from pathlib import Path

from nano_code.messages import ChatMessage
from nano_code.messages.codec import message_from_json, message_to_json


class SessionStore:
    """Persist one session under a caller-selected private state directory."""

    def __init__(self, state_dir: Path, session_id: str) -> None:
        if not session_id or any(
            char
            not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
            for char in session_id
        ):
            raise ValueError(
                "session_id may contain only letters, numbers, '-' and '_'"
            )
        self.session_id = session_id
        self.session_dir = state_dir / "sessions" / session_id
        self.path = self.session_dir / "transcript.jsonl"
        self._known_ids: set[str] | None = None

    def load(self) -> tuple[ChatMessage, ...]:
        if not self.path.exists():
            self._known_ids = set()
            return ()

        messages: list[ChatMessage] = []
        seen: set[str] = set()
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line)
                    message = message_from_json(raw)
                except (json.JSONDecodeError, ValueError, TypeError) as error:
                    raise ValueError(
                        f"Invalid transcript line {line_number}: {error}"
                    ) from error
                if message.uuid in seen:
                    raise ValueError(f"Duplicate message UUID: {message.uuid}")
                if message.parent_uuid is not None and message.parent_uuid not in seen:
                    raise ValueError(
                        f"Missing parent {message.parent_uuid} for {message.uuid}"
                    )
                seen.add(message.uuid)
                messages.append(message)
        self._known_ids = seen
        return tuple(messages)

    def append(self, message: ChatMessage) -> None:
        """Append one message after validating idempotency and parent order."""

        if self._known_ids is None:
            self.load()
        assert self._known_ids is not None
        if message.uuid in self._known_ids:
            return
        if (
            message.parent_uuid is not None
            and message.parent_uuid not in self._known_ids
        ):
            raise ValueError(f"Unknown parent UUID: {message.parent_uuid}")

        self.session_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(
            self.path,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT,
            0o600,
        )
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            json.dump(message_to_json(message), handle, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._known_ids.add(message.uuid)
