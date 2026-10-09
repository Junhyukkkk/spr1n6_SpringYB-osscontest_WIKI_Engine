import asyncio
from datetime import UTC, datetime

import pytest

from team_wiki_engine.bot.data_gate import MAX_DEFER_S, DataGate
from tests.helpers.clock import ManualClock


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_two_readers_enter_together() -> None:
    async with asyncio.timeout(10):
        gate = DataGate()
        async with gate.reading(timeout_s=1) as a, gate.reading(timeout_s=1) as b:
            assert a and b
            assert gate.readers == 2
        assert gate.readers == 0


async def test_writer_waits_for_readers_then_enters() -> None:
    async with asyncio.timeout(10):
        gate = DataGate()
        entered = asyncio.Event()

        async def write() -> None:
            async with gate.writing():
                entered.set()
                assert gate.writer_active

        async with gate.reading(timeout_s=1):
            task = asyncio.create_task(write())
            await _settle()
            assert not entered.is_set()
            assert not gate.writer_active
        await task
        assert entered.is_set()
        assert not gate.writer_active


async def test_reader_during_write_waits_then_enters() -> None:
    async with asyncio.timeout(10):
        gate = DataGate()
        got: list[bool] = []

        async def read() -> None:
            async with gate.reading(timeout_s=5) as ok:
                got.append(ok)

        async with gate.writing():
            task = asyncio.create_task(read())
            await _settle()
            assert got == []
        await task
        assert got == [True]
        assert gate.readers == 0


async def test_reader_times_out_during_write() -> None:
    async with asyncio.timeout(10):
        gate = DataGate()
        async with gate.writing():
            async with gate.reading(timeout_s=0.05) as ok:
                assert ok is False
                assert gate.readers == 0
        assert not gate.writer_active


async def test_overlapping_readers_cannot_starve_writer_past_max_defer() -> None:
    async with asyncio.timeout(10):
        clock = ManualClock(datetime(2026, 10, 7, tzinfo=UTC))
        gate = DataGate(max_defer_s=100, monotonic=clock.monotonic)
        order: list[str] = []

        async def write() -> None:
            async with gate.writing():
                order.append("writer")

        async def late_read() -> None:
            async with gate.reading(timeout_s=5) as ok:
                assert ok
                order.append("reader")

        reader = gate.reading(timeout_s=1)
        assert await reader.__aenter__()
        writer = asyncio.create_task(write())
        await _settle()
        # 마감 전: 새 읽기는 바로 들어간다(쓰기를 기다리게 하지 않는다)
        async with gate.reading(timeout_s=1) as ok:
            assert ok
        assert not gate.writer_waiting
        clock.advance(100)
        late = asyncio.create_task(late_read())
        await _settle()
        assert gate.writer_waiting
        assert order == []  # 새 읽기가 기다린다
        await reader.__aexit__(None, None, None)
        await asyncio.gather(writer, late)
        assert order == ["writer", "reader"]
        assert not gate.writer_waiting


async def test_deadline_is_checked_when_reader_leaves() -> None:
    async with asyncio.timeout(10):
        clock = ManualClock(datetime(2026, 10, 7, tzinfo=UTC))
        gate = DataGate(max_defer_s=10, monotonic=clock.monotonic)
        first = gate.reading(timeout_s=1)
        second = gate.reading(timeout_s=1)
        await first.__aenter__()
        await second.__aenter__()
        writer = asyncio.create_task(_write(gate))
        await _settle()
        clock.advance(10)
        await first.__aexit__(None, None, None)
        assert gate.writer_waiting
        await second.__aexit__(None, None, None)
        await writer


async def _write(gate: DataGate) -> None:
    async with gate.writing():
        pass


def test_default_max_defer() -> None:
    assert MAX_DEFER_S == 1800.0


async def test_cancelled_waiting_reader_and_writer_restore_state() -> None:
    async with asyncio.timeout(10):
        gate = DataGate()
        async with gate.writing():
            reader = asyncio.create_task(_read_wait(gate))
            await _settle()
            reader.cancel()
            with pytest.raises(asyncio.CancelledError):
                await reader
            assert gate.readers == 0
        assert (gate.readers, gate.writer_active, gate.writer_waiting) == (0, False, False)

        held = gate.reading(timeout_s=1)
        await held.__aenter__()
        writer = asyncio.create_task(_write(gate))
        await _settle()
        writer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await writer
        assert gate.readers == 1
        assert not gate.writer_active and not gate.writer_waiting
        await held.__aexit__(None, None, None)
        assert gate.readers == 0


async def _read_wait(gate: DataGate) -> None:
    async with gate.reading(timeout_s=5):
        pass


async def test_cancelled_writer_after_priority_releases_blocked_reader() -> None:
    async with asyncio.timeout(10):
        clock = ManualClock(datetime(2026, 10, 7, tzinfo=UTC))
        gate = DataGate(max_defer_s=10, monotonic=clock.monotonic)
        held = gate.reading(timeout_s=1)
        await held.__aenter__()
        writer = asyncio.create_task(_write(gate))
        await _settle()
        clock.advance(10)
        late = asyncio.create_task(_read_wait(gate))
        await _settle()
        assert gate.writer_waiting
        writer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await writer
        await late  # 쓰기 우선이 풀려 들어간다
        assert not gate.writer_waiting
        await held.__aexit__(None, None, None)


async def test_exception_in_writer_clears_flags_and_releases_reader() -> None:
    async with asyncio.timeout(10):
        gate = DataGate()
        got: list[bool] = []

        async def read() -> None:
            async with gate.reading(timeout_s=5) as ok:
                got.append(ok)

        task: asyncio.Task[None] | None = None
        with pytest.raises(RuntimeError):
            async with gate.writing():
                task = asyncio.create_task(read())
                await _settle()
                raise RuntimeError("x")
        assert task is not None
        await task
        assert got == [True]
        assert not gate.writer_active
