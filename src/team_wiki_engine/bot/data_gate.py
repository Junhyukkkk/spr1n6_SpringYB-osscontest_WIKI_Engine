"""`data` 사본 관문: 답(읽기)은 여럿이 함께, 갱신(쓰기)은 혼자 — 읽기가 모두 나간 뒤에만.

쓰기는 기본적으로 **답 사이에만** 들어간다(새 읽기를 막지 않는다). 그러나 읽기가 끊임없이 겹쳐
쓰기가 `max_defer_s` 넘게 밀리면 "쓰기 우선"(`writer_waiting`)을 켜서 새 읽기를 기다리게 한다.
실제 시간 타이머를 쓰지 않는다: 마감은 새 읽기가 들어오려 할 때와 읽기가 나갈 때 단조 시계로
확인한다(테스트는 `ManualClock`을 넘긴다).

나갈 때의 상태 정리는 동기로 끝내고, 기다리는 쪽을 깨우는 일만 `run_to_completion`으로 취소에서
지킨다(취소돼도 `readers`·플래그가 어긋나지 않는다).
"""

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Final

from team_wiki_engine.util.aio import run_to_completion

MAX_DEFER_S: Final = 1800.0


class DataGate:
    def __init__(
        self,
        *,
        max_defer_s: float = MAX_DEFER_S,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_defer_s = max_defer_s
        self._monotonic = monotonic
        self._cond = asyncio.Condition()
        self._wait_since: float | None = None
        self._waiting_writers = 0
        self.readers = 0
        self.writer_active = False
        self.writer_waiting = False

    # ------------------------------------------------------------ 내부

    def _refresh_priority(self) -> None:
        since = self._wait_since
        if since is not None and self._monotonic() - since >= self._max_defer_s:
            self.writer_waiting = True

    def _reader_can_enter(self) -> bool:
        self._refresh_priority()
        return not self.writer_active and not self.writer_waiting

    async def _notify(self) -> None:
        async with self._cond:
            self._cond.notify_all()

    # ------------------------------------------------------------ 읽기

    def reading(self, *, timeout_s: float) -> AbstractAsyncContextManager[bool]:
        return self._reading(timeout_s)

    @asynccontextmanager
    async def _reading(self, timeout_s: float) -> AsyncIterator[bool]:
        entered = await self._enter_reader(timeout_s)
        try:
            yield entered
        finally:
            if entered:
                self.readers -= 1
                self._refresh_priority()
                await run_to_completion(self._notify())

    async def _enter_reader(self, timeout_s: float) -> bool:
        if self._reader_can_enter():
            self.readers += 1
            return True
        counted = False
        try:
            async with asyncio.timeout(timeout_s):
                async with self._cond:
                    await self._cond.wait_for(self._reader_can_enter)
                    self.readers += 1
                    counted = True
            return True
        except TimeoutError:
            if counted:  # 들어온 직후 마감이 겹쳤다: 쥔 것을 돌려놓는다
                self.readers -= 1
                await run_to_completion(self._notify())
            return False

    # ------------------------------------------------------------ 쓰기

    def writing(self) -> AbstractAsyncContextManager[None]:
        return self._writing()

    @asynccontextmanager
    async def _writing(self) -> AsyncIterator[None]:
        await self._enter_writer()
        try:
            yield
        finally:
            self.writer_active = False
            self.writer_waiting = False
            await run_to_completion(self._notify())

    async def _enter_writer(self) -> None:
        try:
            async with self._cond:
                if self._waiting_writers == 0:
                    self._wait_since = self._monotonic()
                self._waiting_writers += 1
                try:
                    await self._cond.wait_for(lambda: self.readers == 0 and not self.writer_active)
                    self.writer_active = True
                finally:
                    self._waiting_writers -= 1
                    if self._waiting_writers == 0:
                        self._wait_since = None
                        self.writer_waiting = False
        except BaseException:
            if not self.writer_active:
                await run_to_completion(self._notify())  # 쓰기 우선에 막혀 있던 읽기를 깨운다
            raise
