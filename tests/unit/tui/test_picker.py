from my_code.tui.picker import PickerRow, PickerState


def _rows(*keys: str) -> tuple[PickerRow, ...]:
    return tuple(PickerRow(key, key) for key in keys)


def test_picker_stops_at_boundaries_and_scrolls_selection_into_view() -> None:
    rows = _rows(*(str(index) for index in range(10)))
    state = PickerState()

    state.move(rows, -1)
    assert state.index == 0
    for _ in range(20):
        state.move(rows, 1)

    start, visible = state.visible(rows, 7)
    assert state.index == 9
    assert start == 3
    assert [row.key for row in visible] == [str(index) for index in range(3, 10)]


def test_picker_preserves_stable_selection_when_rows_reorder() -> None:
    state = PickerState(index=1)
    original = _rows("first", "selected", "third")
    state.visible(original, 2)

    reordered = _rows("selected", "third", "first")
    state.sync(reordered)

    assert state.index == 0
    assert state.current(reordered) == PickerRow("selected", "selected")
