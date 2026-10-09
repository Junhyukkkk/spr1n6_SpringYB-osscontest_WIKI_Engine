"""봇 수명 주기: 시작 갱신 → 연결 → (신호) → 정해진 종료 순서.

종료 순서(바깥 취소에서도 돈다): STOPPING → 새 메시지 받지 않음 → 갱신·워치독 멈춤 → 진행 중 답
기다림 → 남은 답 취소 → `drain_background_tasks` 정확히 한 번 → 종료 알림 → 연결 닫기.
알림 문구에는 예외 종류 이름만 싣는다(`GatewayConfigError` 메시지는 안전한 고정 문구다).
"""

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from team_wiki_engine.bot.answer import AnswerPipeline
from team_wiki_engine.bot.dispatch import Dispatcher, InFlight
from team_wiki_engine.bot.ports import (
    Gateway,
    GatewayConfigError,
    IncomingMessage,
    MessageReplies,
)
from team_wiki_engine.bot.refresh import DataRefresher
from team_wiki_engine.bot.systemd import sd_notify, watchdog_interval, watchdog_loop
from team_wiki_engine.claude.runner import drain_background_tasks
from team_wiki_engine.ops.notify import Notifier
from team_wiki_engine.sync.claude_step import Drain

log = logging.getLogger(__name__)

SHUTDOWN_GRACE_S: Final = 60.0
CANCEL_WAIT_S: Final = 10.0
GATEWAY_CLOSE_WAIT_S: Final = 10.0
REFRESH_STOP_WAIT_S: Final = 15.0


@dataclass(frozen=True)
class BotDeps:
    pipeline: AnswerPipeline
    dispatcher: Dispatcher
    refresher: DataRefresher
    in_flight: InFlight
    notifier: Notifier
    gateway: Gateway
    env: Mapping[str, str] = field(repr=False)
    drain: Drain = drain_background_tasks
    settle_timeout_s: float = 30.0


def _consume(task: asyncio.Task[object]) -> None:
    if not task.cancelled():
        task.exception()  # 회수만 한다(미회수 예외 경고 방지)


class _Handler:
    """디스코드 핸들러: 첫 `on_ready`에서만 READY와 연결 알림을 낸다."""

    def __init__(self, deps: BotDeps) -> None:
        self._d = deps
        self._ready = False

    async def on_ready(self, bot_user_id: str) -> None:
        await self._d.dispatcher.on_ready(bot_user_id)
        if self._ready:
            return
        self._ready = True  # await 전에 세운다
        sd_notify("READY=1", self._d.env)
        await _send(self._d.notifier, "[bot] 디스코드에 연결했습니다")

    async def on_message(self, message: IncomingMessage, replies: MessageReplies) -> None:
        await self._d.dispatcher.on_message(message, replies)


async def _send(notifier: Notifier, text: str, *, mention_admin: bool = False) -> None:
    try:
        await notifier.send(text, mention_admin=mention_admin)
    except Exception as exc:
        log.warning("운영 알림을 보내지 못했습니다 (%s)", type(exc).__name__)


class BotApp:
    def __init__(self, deps: BotDeps) -> None:
        self._d = deps

    async def run(self, stop: asyncio.Event) -> int:
        d = self._d
        loop_tasks: list[asyncio.Task[object]] = []
        gateway_task: asyncio.Task[None] | None = None
        stop_task: asyncio.Task[bool] | None = None
        code = 0
        fatal: tuple[str, bool] | None = None
        try:
            try:
                await d.refresher.refresh_once()
            except Exception as exc:
                log.warning("시작 갱신 오류 (%s)", type(exc).__name__)
            refresh_task = asyncio.create_task(d.refresher.run_forever(), name="refresh")
            refresh_task.add_done_callback(_consume)
            loop_tasks.append(refresh_task)
            interval = watchdog_interval(d.env)
            if interval is not None:
                dog = asyncio.create_task(watchdog_loop(d.env, interval), name="watchdog")
                dog.add_done_callback(_consume)
                loop_tasks.append(dog)
            gateway_task = asyncio.create_task(d.gateway.run(_Handler(d)), name="gateway")
            stop_task = asyncio.create_task(stop.wait(), name="stop")
            await asyncio.wait({stop_task, gateway_task}, return_when=asyncio.FIRST_COMPLETED)
            if not stop.is_set():
                code, fatal = self._gateway_ended(gateway_task)
        finally:
            await self._shutdown(loop_tasks, gateway_task, stop_task, fatal)
        return code

    @staticmethod
    def _gateway_ended(task: asyncio.Task[None]) -> tuple[int, tuple[str, bool]]:
        exc = None if task.cancelled() else task.exception()
        if isinstance(exc, GatewayConfigError):
            return 2, (f"[bot] 디스코드 설정 오류: {exc}", True)
        name = type(exc).__name__ if exc is not None else "정상 반환"
        return 1, (f"[bot] 디스코드 연결이 끝났습니다({name})", True)

    async def _shutdown(
        self,
        loop_tasks: list[asyncio.Task[object]],
        gateway_task: asyncio.Task[None] | None,
        stop_task: asyncio.Task[bool] | None,
        fatal: tuple[str, bool] | None,
    ) -> None:
        d = self._d
        sd_notify("STOPPING=1", d.env)
        d.dispatcher.close()  # in_flight도 닫는다
        for task in loop_tasks:
            task.cancel()
        if loop_tasks:
            await asyncio.wait(loop_tasks, timeout=REFRESH_STOP_WAIT_S)  # 그 뒤엔 기다리지 않는다
        if stop_task is not None:
            stop_task.cancel()
        remaining = await d.in_flight.wait(SHUTDOWN_GRACE_S)
        if remaining:
            d.in_flight.cancel_all()
            await d.in_flight.wait(CANCEL_WAIT_S)
        try:
            await d.drain(d.settle_timeout_s)
        except Exception as exc:
            log.warning("정리 대기 오류 (%s)", type(exc).__name__)
        await _send(d.notifier, "[bot] 종료합니다")
        if fatal is not None:
            await _send(d.notifier, fatal[0], mention_admin=fatal[1])
        try:
            await d.gateway.close()
        except Exception as exc:
            log.warning("연결을 닫지 못했습니다 (%s)", type(exc).__name__)
        if gateway_task is not None and not gateway_task.done():
            await asyncio.wait({gateway_task}, timeout=GATEWAY_CLOSE_WAIT_S)
            if not gateway_task.done():
                gateway_task.cancel()
        if gateway_task is not None and gateway_task.done():
            _consume(gateway_task)
