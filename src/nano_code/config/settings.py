"""Explicit runtime configuration derived from CLI options and environment."""

from dataclasses import dataclass
from pathlib import Path

from nano_code.permissions import PermissionMode


@dataclass(frozen=True, slots=True)
class Settings:
    cwd: Path
    model: str
    permission_mode: PermissionMode
    max_turns: int
    max_output_tokens: int
    context_chars: int
    state_dir: Path
    interactive: bool
    api_key: str | None = None
    base_url: str | None = None
