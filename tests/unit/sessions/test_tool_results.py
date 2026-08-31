import hashlib
import stat
from pathlib import Path

import pytest

from my_code.sessions._tool_results import ToolResultStore


def _result_path(root: Path, tool_use_id: str) -> Path:
    digest = hashlib.sha256(tool_use_id.encode("utf-8")).hexdigest()[:20]
    return root / f"{digest}.txt"


def test_large_result_is_written_to_private_ephemeral_store(tmp_path: Path) -> None:
    root = tmp_path / "runtime" / "session" / "tool-results"
    store = ToolResultStore(root)

    projected = store.externalize("call", "x" * 20_001)
    path = _result_path(root, "call")

    assert path.read_text(encoding="utf-8") == "x" * 20_001
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert str(path) in projected
    assert "saved temporarily" in projected


def test_externalization_never_reuses_or_follows_an_existing_path(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime" / "session" / "tool-results"
    root.mkdir(parents=True)
    path = _result_path(root, "call")
    path.write_text("stale", encoding="utf-8")
    store = ToolResultStore(root)

    with pytest.raises(FileExistsError, match="already exists"):
        store.externalize("call", "x" * 20_001)
    assert path.read_text(encoding="utf-8") == "stale"

    path.unlink()
    outside = tmp_path / "outside"
    outside.write_text("secret", encoding="utf-8")
    path.symlink_to(outside)
    with pytest.raises(FileExistsError, match="Unsafe"):
        store.externalize("call", "x" * 20_001)
    assert outside.read_text(encoding="utf-8") == "secret"
