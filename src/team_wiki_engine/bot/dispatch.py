"""메시지 분배: 필터, 관리자 명령, 정지 스위치, 진행 중 작업, 중복 제거.

디스코드를 모른다(`IncomingMessage`와 `MessageReplies`만 본다). `on_message`의 판정과 스레드 점유는
`await` 없이 끝낸다 — 같은 스레드에 연달아 온 두 메시지가 서로를 앞지르지 못하게 한다.
로그에는 예외 종류 이름만 싣는다(메시지·질문·사용자 ID 없음).
"""

import asyncio
import logging
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, TypeAdapter

from team_wiki_engine.bot.answer import DISCORD_CALL_TIMEOUT_S, AnswerPipeline
from team_wiki_engine.bot.limiter import Limiter
from team_wiki_engine.bot.ports import AnswerThread, IncomingMessage, MessageReplies
from team_wiki_engine.bot.refresh import RefreshState
from team_wiki_engine.bot.replies import (
    ADMIN_ONLY_TEXT,
    PAUSE_ACK_TEXT,
    PAUSED_TEXT,
    RESUME_ACK_TEXT,
    TOO_LONG_TEXT,
    USAGE_TEXT,
    rejection_text,
)
from team_wiki_engine.bot.text import (
    extract_question,
    hash_user,
    is_snowflake,
    mentions_bot,
    normalize_question,
    question_too_long,
)
from team_wiki_engine.metrics.record import read_json, write_json
from team_wiki_engine.ops.notify import Notifier
from team_wiki_engine.util.time import kst_iso, kst_parts

log = logging.getLogger(__name__)

MAX_SEEN_MESSAGES: Final = 1024
MAX_PENDING_REPLIES: Final = 8

AdminCommand = Literal["pause", "resume", "status"]
ADMIN_COMMANDS: Final[Mapping[str, AdminCommand]] = {
    "!정지": "pause",
    "!재개": "resume",
    "!상태": "status",
}


@dataclass(frozen=True)
class BotSettings:
    guild_id: str
    channel_ids: frozenset[str]
    admin_user_id: str | None
    ops_channel_id: str | None


def should_handle(message: IncomingMessage, settings: BotSettings, bot_user_id: str) -> bool:
    if message.author_is_bot or message.is_system:
        return False
    if not (
        is_snowflake(message.message_id)
        and is_snowflake(message.author_id)
        and is_snowflake(message.channel_id)
    ):
        return False
    if message.guild_id is None or message.guild_id != settings.guild_id:
        return False
    parent = message.parent_channel_id
    if parent is not None and not is_snowflake(parent):
        return False
    if message.channel_id not in settings.channel_ids and (
        parent is None or parent not in settings.channel_ids
    ):
        return False
    return mentions_bot(message.content, bot_user_id)


def parse_admin_command(question: str) -> AdminCommand | None:
    return ADMIN_COMMANDS.get(question)


class BotControl(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="ignore")

    paused: bool
    changed_at: str
    changed_by: str


_CONTROL_ADAPTER: Final[TypeAdapter[BotControl | None]] = TypeAdapter(BotControl | None)


class PauseSwitch:
    def __init__(self, file: Path, *, now: Callable[[], datetime]) -> None:
        self._file = file
        self._now = now
        control = read_json(file, _CONTROL_ADAPTER, None)
        self._paused = control is not None and control.paused

    @property
    def paused(self) -> bool:
        return self._paused

    def set(self, paused: bool, by: str) -> None:
        write_json(
            self._file, BotControl(paused=paused, changed_at=kst_iso(self._now()), changed_by=by)
        )
        self._paused = paused


class InFlight:
    def __init__(self, max_tasks: int) -> None:
        self._max = max_tasks
        self._tasks: set[asyncio.Task[object]] = set()
        self.closed = False

    def __len__(self) -> int:
        return sum(1 for t in self._tasks if not t.done())

    def spawn(self, coro: Coroutine[Any, Any, object], *, name: str) -> bool:
        if self.closed or len(self) >= self._max:
            coro.close()
            return False
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._done)
        return True

    def _done(self, task: asyncio.Task[object]) -> None:
        self._tasks.discard(task)
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                log.warning("진행 중 작업이 오류로 끝났습니다 (%s)", type(exc).__name__)

    def close(self) -> None:
        self.closed = True

    async def wait(self, timeout_s: float) -> int:
        pending = {t for t in self._tasks if not t.done()}
        if pending:
            await asyncio.wait(pending, timeout=timeout_s)  # 시간이 지나도 취소하지 않는다
        return len(self)

    def cancel_all(self) -> None:
        for task in list(self._tasks):
            task.cancel()


@dataclass(frozen=True)
class StatusSnapshot:
    paused: bool
    in_flight: int
    daily_used: int
    daily_total: int
    cost_today: float
    refresh: RefreshState


def status_text(s: StatusSnapshot) -> str:
    if s.refresh.last_ok_at is None:
        refresh = "위키 사본 갱신: 성공 기록 없음"
    else:
        parts = kst_parts(s.refresh.last_ok_at)
        refresh = f"위키 사본 갱신: {parts.date} {parts.time} 성공"
    if s.refresh.consecutive_failures > 0:
        refresh += f" · 이후 실패 {s.refresh.consecutive_failures}회"
    return "\n".join(
        [
            "상태: 멈춤" if s.paused else "상태: 답변 중",
            f"진행 중 질문: {s.in_flight}",
            f"오늘 질문: {s.daily_used} / {s.daily_total}",
            f"오늘 비용: ${s.cost_today:.2f}",
            refresh,
        ]
    )


@dataclass(frozen=True)
class _Ctx:
    user_id: str
    question: str
    existing_thread_id: str | None
    replies: MessageReplies

    async def open_thread(self, name: str) -> AnswerThread:
        return await self.replies.open_thread(name)

    async def reply_in_place(self, text: str) -> None:
        await self.replies.reply_in_place(text)


class Dispatcher:
    def __init__(
        self,
        *,
        settings: BotSettings,
        pipeline: AnswerPipeline,
        limiter: Limiter,
        pause: PauseSwitch,
        in_flight: InFlight,
        refresh_state: Callable[[], RefreshState],
        notifier: Notifier,
        user_salt: str,
    ) -> None:
        self._settings = settings
        self._pipeline = pipeline
        self._limiter = limiter
        self._pause = pause
        self._in_flight = in_flight
        self._refresh_state = refresh_state
        self._notifier = notifier
        self._salt = user_salt
        self._bot_id: str | None = None
        self._seen: dict[str, None] = {}
        self._closed = False

    async def on_ready(self, bot_user_id: str) -> None:
        if is_snowflake(bot_user_id):
            self._bot_id = bot_user_id

    def close(self) -> None:
        self._closed = True
        self._in_flight.close()

    # ------------------------------------------------------------ 작은 도우미
    async def _reply(self, replies: MessageReplies, text: str, notice: str | None = None) -> None:
        try:
            async with asyncio.timeout(DISCORD_CALL_TIMEOUT_S):
                await replies.reply_in_place(text)
        except Exception as exc:
            log.warning("제자리 안내를 보내지 못했습니다 (%s)", type(exc).__name__)
        if notice is not None:
            try:
                await self._notifier.send(notice)
            except Exception as exc:
                log.warning("운영 알림을 보내지 못했습니다 (%s)", type(exc).__name__)

    def _say(self, replies: MessageReplies, text: str, notice: str | None = None) -> None:
        self._in_flight.spawn(self._reply(replies, text, notice), name="reply")

    def _remember(self, message_id: str) -> bool:
        if message_id in self._seen:
            return False
        self._seen[message_id] = None
        while len(self._seen) > MAX_SEEN_MESSAGES:
            del self._seen[next(iter(self._seen))]
        return True

    def _admin(self, command: AdminCommand, admin: str, replies: MessageReplies) -> None:
        if command == "status":
            snap = StatusSnapshot(
                paused=self._pause.paused,
                in_flight=len(self._in_flight),
                daily_used=self._limiter.daily_used(),
                daily_total=self._limiter.daily_total,
                cost_today=self._pipeline.cost_today(),
                refresh=self._refresh_state(),
            )
            self._say(replies, status_text(snap))
            return
        pausing = command == "pause"
        try:
            self._pause.set(pausing, hash_user(admin, self._salt))
        except (OSError, ValueError) as exc:
            log.warning("정지 상태를 쓰지 못했습니다 (%s)", type(exc).__name__)
            self._say(replies, rejection_text("busy"))
            return
        if pausing:
            self._say(replies, PAUSE_ACK_TEXT, "[bot] 관리자가 답변을 멈췄습니다")
        else:
            self._say(replies, RESUME_ACK_TEXT, "[bot] 관리자가 답변을 다시 시작했습니다")

    # ------------------------------------------------------------ 메시지
    async def on_message(self, message: IncomingMessage, replies: MessageReplies) -> None:
        try:
            self._dispatch(message, replies)
        except Exception as exc:
            log.warning("메시지 처리 중 오류 (%s)", type(exc).__name__)

    def _dispatch(self, m: IncomingMessage, replies: MessageReplies) -> None:
        bot_id = self._bot_id
        if self._closed or bot_id is None:
            return
        if not should_handle(m, self._settings, bot_id):
            return
        if not self._remember(m.message_id):
            return
        raw = extract_question(m.content, bot_id)
        if question_too_long(raw):
            self._say(replies, TOO_LONG_TEXT)
            return
        question = normalize_question(raw)
        command = parse_admin_command(question)
        if command is not None:
            admin = self._settings.admin_user_id
            if admin is None or admin == "" or m.author_id != admin:
                self._say(replies, ADMIN_ONLY_TEXT)
            else:
                self._admin(command, admin, replies)
            return
        if question == "":
            self._say(replies, USAGE_TEXT)
            return
        if self._pause.paused:
            self._say(replies, PAUSED_TEXT)
            return
        existing: str | None = None
        if m.parent_channel_id is not None:
            if not self._pipeline.claim_thread(m.channel_id):
                self._say(replies, rejection_text("user_busy"))
                return
            existing = m.channel_id
        ctx = _Ctx(m.author_id, question, existing, replies)
        if not self._in_flight.spawn(self._pipeline.handle(ctx), name="answer"):
            if existing is not None:
                self._pipeline.release_thread(existing)
            log.warning("진행 중 작업이 가득 차 질문을 버렸습니다")
