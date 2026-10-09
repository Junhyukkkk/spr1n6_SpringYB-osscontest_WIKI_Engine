"""run_to_completion·settle: 바깥이 몇 번 취소돼도 안쪽은 끝까지 간다."""

import asyncio

import pytest

from team_wiki_engine.util.aio import run_to_completion, settle


class TestRunToCompletion:
    async def test_결과를_돌려준다(self) -> None:
        async def work() -> int:
            await asyncio.sleep(0)
            return 7

        assert await run_to_completion(work()) == 7

    async def test_안쪽_예외는_전파한다(self) -> None:
        async def work() -> int:
            raise ValueError("x")

        with pytest.raises(ValueError):
            await run_to_completion(work())

    async def test_여러_번_취소돼도_끝까지_돌린_뒤_취소를_전파한다(self) -> None:
        gate = asyncio.Event()
        entered = asyncio.Event()
        finished: list[int] = []

        async def inner() -> None:
            entered.set()
            await gate.wait()
            finished.append(1)

        async def outer() -> None:
            await run_to_completion(inner())

        task = asyncio.create_task(outer())
        async with asyncio.timeout(10):
            await entered.wait()
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            gate.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert finished == [1]

    async def test_취소와_안쪽_실패가_겹치면_취소가_이긴다(self) -> None:
        gate = asyncio.Event()
        entered = asyncio.Event()

        async def inner() -> None:
            entered.set()
            await gate.wait()
            raise ValueError("inner")

        task = asyncio.create_task(run_to_completion(inner()))
        async with asyncio.timeout(10):
            await entered.wait()
            task.cancel()
            await asyncio.sleep(0)
            gate.set()
            with pytest.raises(asyncio.CancelledError):
                await task


class TestSettle:
    async def test_태스크와_취소_여부를_돌려준다(self) -> None:
        async def work() -> int:
            return 1

        task, cancelled = await settle(work())
        assert task.result() == 1 and cancelled is False
