"""Append-only JSONL transcripts with parent-chain validation."""

import json
import os
import re
from pathlib import Path

from nano_code.messages import ChatMessage
from nano_code.messages.codec import message_from_json, message_to_json

_UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


class SessionStore:
    """Persist one session in a Claude Code-style per-project directory."""

    def __init__(self, project_state_dir: Path, session_id: str) -> None:
        if _UUID_PATTERN.fullmatch(session_id) is None:
            raise ValueError("session_id must be a UUID")
        self.session_id = session_id
        self.project_state_dir = project_state_dir
        # The transcript is a project-directory sibling of the session folder;
        # large tool results live below the latter in ``tool-results/``.
        self.path = project_state_dir / f"{session_id}.jsonl"
        self.session_dir = project_state_dir / session_id

        # This set is both an idempotency guard and a cheap parent-existence index.
        # It is initialized from disk so resumed and newly-created stores behave alike.
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

                # Append-only discipline requires parents to appear first. Rejecting
                # a dangling edge is safer than silently returning a truncated chain.
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
            # Agent iterations may revisit the same in-memory prefix. UUID-based
            # idempotency prevents it from being appended again on every iteration.
            return
        if (
            message.parent_uuid is not None
            and message.parent_uuid not in self._known_ids
        ):
            raise ValueError(f"Unknown parent UUID: {message.parent_uuid}")

        # Session data can include source code and command output, so create both
        # directory and file with owner-only permissions.
        self.project_state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(
            self.path,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT,
            0o600,
        )
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            # One self-contained JSON value per line confines an interrupted write
            # to a diagnosable final record instead of corrupting the whole file.
            json.dump(message_to_json(message), handle, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        # Update the in-memory index only after the durable write succeeds.
        self._known_ids.add(message.uuid)
