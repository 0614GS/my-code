import asyncio
from pathlib import Path

import pytest

from my_code.chat.pending_inputs import PendingInputController, QueueInputState
from my_code.conversation.attachments import FileMentionAttachment
from my_code.conversation.models import AttachmentMessage, HumanMessage
from my_code.sessions.session import Session
from my_code.tui.rendering import StreamingMarkdownProjector


class _Loaded:
    def __init__(self, attachment: FileMentionAttachment) -> None:
        self.attachment = attachment


class _Loader:
    async def load(self, prompt: str):
        if prompt == "bad":
            raise OSError("cannot read attachment")
        await asyncio.sleep(0)
        return (_Loaded(FileMentionAttachment("note.txt", "hello")),)


@pytest.mark.asyncio
async def test_pending_inputs_prepare_commit_then_leave_queue() -> None:
    controller = PendingInputController("session", _Loader())  # type: ignore[arg-type]
    first = controller.queue_input("first")
    second = controller.queue_input("second")

    inputs = await controller.drain_pending()

    assert [item.prompt for item in inputs] == ["first", "second"]
    assert all(item.attachments for item in inputs)
    assert len(controller.queued_inputs()) == 2
    controller.accept_pending((first.input_id, second.input_id))
    assert controller.queued_inputs() == ()


@pytest.mark.asyncio
async def test_failed_input_stays_recallable() -> None:
    controller = PendingInputController("session", _Loader())  # type: ignore[arg-type]
    controller.queue_input("bad")

    await controller.prepare_pending()

    view = controller.queued_inputs()[0]
    assert view.state is QueueInputState.FAILED
    assert "cannot read" in (view.error or "")
    assert controller.recall_latest_input() == "bad"
    assert controller.queued_inputs() == ()


def test_commit_user_inputs_is_one_persistence_first_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = Session(tmp_path / "sessions", "11111111-1111-1111-1111-111111111111")
    attachment = FileMentionAttachment("note.txt", "hello")

    session.commit_user_inputs((("one", (attachment,)), ("two", ())))

    assert [type(item) for item in session.conversation] == [
        HumanMessage,
        AttachmentMessage,
        HumanMessage,
    ]
    restored = Session.restore(tmp_path / "sessions", session.session_id)
    assert [type(item) for item in restored.conversation] == [
        HumanMessage,
        AttachmentMessage,
        HumanMessage,
    ]

    before = session.conversation

    def fail(_messages) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(session._store, "append_message_batch", fail)
    with pytest.raises(OSError, match="disk full"):
        session.commit_user_inputs((("not committed", ()),))
    assert session.conversation == before


def test_streaming_markdown_projection_has_strict_input_and_line_bounds() -> None:
    projector = StreamingMarkdownProjector()
    source = ("paragraph\n\n" * 5000) + ("x" * (20 * 1024))

    frame = projector.project(source, 80)

    assert projector.last_rich_input_chars == 0
    assert len("".join(fragment[1] for fragment in frame).splitlines()) <= 12
    assert "x" in "".join(fragment[1] for fragment in frame)
