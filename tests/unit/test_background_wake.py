"""Monotonic, payload-free background task wake signal coverage."""

import asyncio

import pytest

from my_code.features.subagents.wake import BackgroundTaskWakeSignal


@pytest.mark.asyncio
async def test_wait_observes_pulse_between_revision_check_and_wait() -> None:
    signal = BackgroundTaskWakeSignal()
    revision = signal.revision

    waiter = asyncio.create_task(signal.wait_for_change(revision))
    signal.pulse()

    assert await asyncio.wait_for(waiter, timeout=1) == 1


@pytest.mark.asyncio
async def test_pulses_are_monotonic_and_may_be_coalesced() -> None:
    signal = BackgroundTaskWakeSignal()

    signal.pulse()
    signal.pulse()

    assert signal.revision == 2
    assert await signal.wait_for_change(0) == 2
    assert vars(signal).keys() == {"_revision", "_changed"}
