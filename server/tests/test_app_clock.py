from __future__ import annotations

import asyncio
import threading

import pytest

from server.app.clock import AsyncioClock
from server.core.events import TimerFired


def test_schedule_fires_timer_fired_after_the_timeout() -> None:
    async def scenario() -> None:
        clock = AsyncioClock()
        clock.schedule("s1", 0.01)

        assert await asyncio.wait_for(clock.recv(), 1.0) == TimerFired(
            connection_id="s1"
        )
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(clock.recv(), 0.05)

    asyncio.run(scenario())


def test_rescheduling_rearms_the_timeout() -> None:
    async def scenario() -> None:
        clock = AsyncioClock()
        clock.schedule("s1", 0.05)
        clock.schedule("s1", 0.01)

        assert await asyncio.wait_for(clock.recv(), 1.0) == TimerFired(
            connection_id="s1"
        )
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(clock.recv(), 0.1)

    asyncio.run(scenario())


def test_cancel_prevents_the_timeout_from_firing() -> None:
    async def scenario() -> None:
        clock = AsyncioClock()
        clock.schedule("s1", 0.01)
        clock.cancel("s1")

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(clock.recv(), 0.05)

    asyncio.run(scenario())


def test_close_cancels_pending_timeouts() -> None:
    async def scenario() -> None:
        clock = AsyncioClock()
        clock.schedule("s1", 0.01)
        clock.close()

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(clock.recv(), 0.05)

    asyncio.run(scenario())


def test_timeout_fires_on_the_single_loop_without_spawning_a_thread() -> None:
    async def scenario() -> None:
        clock = AsyncioClock()
        threads_before = threading.active_count()
        clock.schedule("s1", 0.01)

        assert await asyncio.wait_for(clock.recv(), 1.0) == TimerFired(
            connection_id="s1"
        )
        assert threading.active_count() == threads_before

    asyncio.run(scenario())
