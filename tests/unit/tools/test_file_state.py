from pathlib import Path

from my_code.tools.file_state import RecentFileRegistry
from my_code.workspace.local import FileFingerprint


def _fingerprint(value: int) -> FileFingerprint:
    return FileFingerprint(value, value, value, value, f"{value:064x}")


def test_recent_files_are_deduplicated_ordered_bounded_and_session_isolated(
    tmp_path: Path,
) -> None:
    registry = RecentFileRegistry(max_files_per_session=3)
    paths = tuple(tmp_path / f"{index}.txt" for index in range(5))

    for index, path in enumerate(paths[:4]):
        registry.record("one", path, _fingerprint(index))
    registry.record("one", paths[1], _fingerprint(10))
    registry.record("two", paths[4], _fingerprint(4))

    assert tuple(item.path for item in registry.recent("one")) == (
        paths[1],
        paths[3],
        paths[2],
    )
    assert registry.recent("one")[0].fingerprint == _fingerprint(10)
    assert tuple(item.path for item in registry.recent("two")) == (paths[4],)

    registry.clear()
    assert registry.recent("one") == ()
    assert registry.recent("two") == ()
