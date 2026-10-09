"""질문 처리 흐름: 사용량 제한 → 스레드 → `data` 읽기 관문 → Claude → 답 가리기·나누기 → 기록.

로그에는 질문·답 원문, 사용자 ID 원문, `RunClaudeResult.detail`, 예외 메시지를 싣지 않는다
(사용자 해시·스레드 ID·결과·예외 종류 이름·길이뿐이다).
"""

import asyncio
import dataclasses
import logging
import math
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Literal

from team_wiki_engine.bot.data_gate import DataGate
from team_wiki_engine.bot.limiter import Limiter, Rejected, RejectReason
from team_wiki_engine.bot.ports import AnswerThread, QuestionContext
from team_wiki_engine.bot.prompt import build_bot_prompt, new_nonce, read_basis
from team_wiki_engine.bot.refresh import data_ready
from team_wiki_engine.bot.replies import (
    CODE_SOURCE_NOTE,
    DATA_BUSY_TEXT,
    RESUME_FAILED_PREFIX,
    SHUTDOWN_TEXT,
    failure_text,
    rejection_text,
)
from team_wiki_engine.bot.sessions import SessionStore
from team_wiki_engine.bot.split import MAX_CHUNKS, cap_chunks, split_message
from team_wiki_engine.bot.text import (
    answer_roots,
    evidence_not_found,
    hash_user,
    mask_known,
    record_text,
    safe_answer,
    thread_name,
)
from team_wiki_engine.claude.types import ClaudeRunner, RunClaudeInput, RunClaudeResult
from team_wiki_engine.config.load import Paths
from team_wiki_engine.config.schema import TeamConfig
from team_wiki_engine.metrics.answer_source import answer_source, code_paths
from team_wiki_engine.metrics.record import STATE_FILES, GapRecord, QuestionRecord, append_jsonl
from team_wiki_engine.ops.notify import Notifier
from team_wiki_engine.util.time import kst_iso, kst_parts
from team_wiki_engine.wiki.lint_secrets import clean

log = logging.getLogger(__name__)

HandleResult = Literal["answered", "rejected", "failed"]

DATA_WAIT_S: Final = 60.0
POST_TIMEOUT_S: Final = 60.0
DISCORD_CALL_TIMEOUT_S: Final = 30.0
MAX_GAP_PATHS: Final = 20
MAX_GAP_PATH_CHARS: Final = 200
SHUTDOWN_NOTICE_TIMEOUT_S: Final = 5.0
REJECT_REPLY_COOLDOWN_S: Final = 30.0
ERROR_NOTICE_COOLDOWN_S: Final = 600.0
MAX_COOLDOWN_USERS: Final = 10_000


@dataclass(frozen=True)
class AnswerDeps:
    runner: ClaudeRunner
    sessions: SessionStore
    limiter: Limiter
    gate: DataGate
    paths: Paths
    config: TeamConfig
    notifier: Notifier
    now: Callable[[], datetime]
    monotonic: Callable[[], float] = time.monotonic
    nonce: Callable[[], str] = new_nonce
    redact: tuple[str, ...] = field(default=(), repr=False)
    user_salt: str = field(default="", repr=False)


def _num(value: float) -> float:
    return value if math.isfinite(value) and value > 0 else 0.0


def _count(value: int) -> int:
    return max(int(value), 0)


@dataclass
class _Totals:
    cost: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0

    def add(self, r: RunClaudeResult) -> None:
        self.cost += _num(r.cost_usd)
        self.input_tokens += _count(r.usage.input_tokens)
        self.output_tokens += _count(r.usage.output_tokens)
        self.cache_read += _count(r.usage.cache_read_input_tokens)


@dataclass
class _State:
    """흐름 도중의 진행 상황. 취소·예기치 않은 오류 때 기록과 안내에 쓴다."""

    thread: AnswerThread | None = None
    totals: _Totals = field(default_factory=_Totals)
    previous: str | None = None
    resume_failed: bool = False


@dataclass
class _Outcome:
    """관문 안에서 정한 결과. 올리기·기록은 관문을 나와서 한다."""

    post: list[str]
    ok: bool
    failure: str | None
    source: Literal["wiki", "code"] | None = None
    not_found: bool = False
    session_id: str | None = None


class AnswerPipeline:
    def __init__(self, deps: AnswerDeps) -> None:
        self._d = deps
        self._claimed: set[str] = set()
        self._reject_replied: dict[str, float] = {}
        self._quota_notified: str | None = None
        self._last_error_notice: float | None = None
        self._cost_date = kst_parts(deps.now()).date
        self._cost = 0.0

    # ------------------------------------------------------------ 스레드 점유·비용
    def claim_thread(self, thread_id: str) -> bool:
        if thread_id in self._claimed:
            return False
        self._claimed.add(thread_id)
        return True

    def release_thread(self, thread_id: str) -> None:
        self._claimed.discard(thread_id)

    def thread_busy(self, thread_id: str) -> bool:
        return thread_id in self._claimed

    def _roll_cost(self) -> None:
        today = kst_parts(self._d.now()).date
        if today > self._cost_date:
            self._cost_date = today
            self._cost = 0.0

    def cost_today(self) -> float:
        self._roll_cost()
        return self._cost

    def seed_cost_today(self, cost: float) -> None:
        self._roll_cost()
        self._cost = _num(cost)

    def active(self) -> int:
        return self._d.limiter.active()

    # ------------------------------------------------------------ 작은 도우미
    async def _reply(self, ctx: QuestionContext, text: str) -> None:
        try:
            async with asyncio.timeout(DISCORD_CALL_TIMEOUT_S):
                await ctx.reply_in_place(text)
        except Exception as exc:
            log.warning("제자리 안내를 보내지 못했습니다 (%s)", type(exc).__name__)

    async def _open(self, ctx: QuestionContext) -> AnswerThread | None:
        try:
            async with asyncio.timeout(DISCORD_CALL_TIMEOUT_S):
                return await ctx.open_thread(thread_name(mask_known(ctx.question, self._d.redact)))
        except Exception as exc:
            log.warning("질문 처리 실패: 스레드를 열 수 없습니다 (%s)", type(exc).__name__)
            return None

    def _write(self, name: str, record: QuestionRecord | GapRecord) -> None:
        try:
            append_jsonl(self._d.paths.state_dir / name, record)
        except (OSError, ValueError) as exc:
            log.warning("기록을 쓰지 못했습니다 (%s)", type(exc).__name__)

    # ------------------------------------------------------------ 흐름
    async def _notify(self, text: str) -> None:
        try:
            await self._d.notifier.send(text)
        except Exception as exc:
            log.warning("운영 알림을 보내지 못했습니다 (%s)", type(exc).__name__)

    async def _rejected(self, ctx: QuestionContext, reason: RejectReason) -> None:
        d = self._d
        if ctx.existing_thread_id is not None:
            self.release_thread(ctx.existing_thread_id)
        if reason == "daily_quota":
            today = kst_parts(d.now()).date
            if self._quota_notified != today:
                self._quota_notified = today
                await self._notify(f"[bot] 오늘 질문 한도({d.limiter.daily_total}회)에 닿았습니다")
        now = d.monotonic()
        last = self._reject_replied.get(ctx.user_id)
        if last is not None and 0 <= now - last < REJECT_REPLY_COOLDOWN_S:
            return
        if len(self._reject_replied) >= MAX_COOLDOWN_USERS:
            self._reject_replied = {
                u: t
                for u, t in self._reject_replied.items()
                if 0 <= now - t < REJECT_REPLY_COOLDOWN_S
            }
        self._reject_replied[ctx.user_id] = now
        await self._reply(ctx, rejection_text(reason))

    async def handle(self, ctx: QuestionContext) -> HandleResult:
        d = self._d
        acquired = d.limiter.acquire(ctx.user_id)
        if isinstance(acquired, Rejected):
            await self._rejected(ctx, acquired.reason)
            return "rejected"
        claimed = ctx.existing_thread_id
        try:
            return await self._run(ctx)
        finally:
            acquired.slot.release()
            if claimed is not None:
                self.release_thread(claimed)

    async def _run(self, ctx: QuestionContext) -> HandleResult:
        d = self._d
        started_wall = d.now()
        started = d.monotonic()
        state = _State()
        try:
            return await self._flow(ctx, state, started_wall, started)
        except asyncio.CancelledError:
            if state.thread is not None:
                try:
                    async with asyncio.timeout(SHUTDOWN_NOTICE_TIMEOUT_S):
                        await state.thread.post([SHUTDOWN_TEXT])
                except Exception as exc:
                    log.warning("종료 안내를 보내지 못했습니다 (%s)", type(exc).__name__)
            self._record(ctx, state, started_wall, started, ok=False, failure="cancelled")
            raise
        except Exception as exc:
            log.warning("질문 처리 중 예기치 않은 오류 (%s)", type(exc).__name__)
            now = d.monotonic()
            last = self._last_error_notice
            if last is None or not 0 <= now - last < ERROR_NOTICE_COOLDOWN_S:
                self._last_error_notice = now
                await self._notify(f"[bot] 질문 처리 중 오류: {type(exc).__name__}")
            if state.thread is not None:
                await self._post(state.thread, [failure_text("error")])
            else:
                await self._reply(ctx, failure_text("error"))
            self._record(ctx, state, started_wall, started, ok=False, failure="error")
            return "failed"

    def _record(
        self,
        ctx: QuestionContext,
        state: _State,
        started_wall: datetime,
        started: float,
        *,
        ok: bool,
        failure: str | None,
        source: Literal["wiki", "code"] | None = None,
        not_found: bool = False,
    ) -> None:
        d = self._d
        thread = state.thread
        if thread is None:
            return
        t = state.totals
        self._roll_cost()
        self._cost += t.cost
        self._write(
            STATE_FILES.questions,
            QuestionRecord(
                at=kst_iso(started_wall),
                user=hash_user(ctx.user_id, d.user_salt),
                thread_id=thread.thread_id,
                ok=ok,
                failure=failure,
                source=source,
                duration_ms=max(int((d.monotonic() - started) * 1000), 0),
                cost_usd=_num(t.cost),
                input_tokens=t.input_tokens,
                output_tokens=t.output_tokens,
                cache_read_input_tokens=t.cache_read,
                resumed=state.previous is not None and not state.resume_failed,
                not_found=not_found,
            ),
        )

    async def _flow(
        self, ctx: QuestionContext, state: _State, started_wall: datetime, started: float
    ) -> HandleResult:
        d = self._d
        thread = await self._open(ctx)
        if thread is None:
            await self._reply(ctx, failure_text("error"))
            return "failed"
        state.thread = thread
        thread_id = thread.thread_id
        if ctx.existing_thread_id is None:
            self.claim_thread(thread_id)
        try:
            gaps: list[GapRecord] = []
            async with d.gate.reading(timeout_s=DATA_WAIT_S) as entered:
                if not entered or not data_ready(d.paths.data_dir):
                    out = _Outcome([DATA_BUSY_TEXT], False, "data_busy")
                else:
                    out = await self._answer(ctx, thread, started_wall, gaps, state)
            for gap in gaps:
                self._write(STATE_FILES.gaps, gap)
            posted = await self._post(thread, out.post)
            if posted and out.ok and out.session_id:
                try:
                    d.sessions.set(thread_id, out.session_id)
                except (ValueError, OSError) as exc:
                    log.warning("세션을 저장하지 못했습니다 (%s)", type(exc).__name__)
            ok = out.ok and posted
            self._record(
                ctx,
                state,
                started_wall,
                started,
                ok=ok,
                failure=out.failure if posted else "post",
                source=out.source,
                not_found=out.not_found,
            )
            return "answered" if ok else "failed"
        finally:
            if ctx.existing_thread_id is None:
                self.release_thread(thread_id)

    async def _post(self, thread: AnswerThread, chunks: list[str]) -> bool:
        try:
            async with asyncio.timeout(POST_TIMEOUT_S):
                await thread.post(chunks)
        except Exception as exc:
            log.warning("답을 올리지 못했습니다 (%s)", type(exc).__name__)
            return False
        return True

    async def _answer(
        self,
        ctx: QuestionContext,
        thread: AnswerThread,
        started_wall: datetime,
        gaps: list[GapRecord],
        state: _State,
    ) -> _Outcome:
        d = self._d
        thread_id = thread.thread_id
        inp = RunClaudeInput(
            profile="bot",
            prompt=build_bot_prompt(
                question=mask_known(ctx.question, d.redact),
                basis=read_basis(d.paths, d.redact),
                today=kst_parts(d.now()).date,
                nonce=d.nonce(),
            ),
            cwd=d.paths.data_dir,
            read_roots=(d.paths.repos_dir,),
            write_roots=(),
            timeout_ms=d.config.limits.bot_timeout_ms,
        )
        previous = d.sessions.get(thread_id)
        state.previous = previous
        totals = state.totals
        async with thread.typing():
            if previous is not None:
                r = await d.runner(dataclasses.replace(inp, resume_session_id=previous))
                totals.add(r)
                if not r.ok and r.failure == "error":
                    state.resume_failed = True
                    try:
                        d.sessions.remove(thread_id)
                    except (ValueError, OSError) as exc:
                        log.warning("세션을 지우지 못했습니다 (%s)", type(exc).__name__)
                    r = await d.runner(inp)
                    totals.add(r)
            else:
                r = await d.runner(inp)
                totals.add(r)

        def fail(kind: str) -> _Outcome:
            failure = kind if kind in ("timeout", "error", "empty", "max_turns") else "error"
            return _Outcome(
                [failure_text(failure)],  # type: ignore[arg-type]
                False,
                failure,
            )

        if not r.ok:
            return fail(r.failure or "error")
        body = safe_answer(r.text, redact=d.redact, roots=answer_roots(d.paths))
        if body == "":
            return fail("empty")
        source = answer_source(r.tool_calls, d.paths.repos_dir)
        not_found = evidence_not_found(r.text)
        with_note = source == "code" and not not_found
        if with_note:
            gaps.append(
                GapRecord(
                    id=uuid.uuid4().hex,
                    at=kst_iso(started_wall),
                    question=record_text(ctx.question, d.redact),
                    answer_summary=record_text(body, d.redact),
                    code_paths=[
                        clean(p)[:MAX_GAP_PATH_CHARS]
                        for p in code_paths(r.tool_calls, d.paths.repos_dir)[:MAX_GAP_PATHS]
                    ],
                )
            )
        if state.resume_failed:
            body = RESUME_FAILED_PREFIX + "\n\n" + body
        chunks = split_message(body)
        if with_note:
            chunks = [*cap_chunks(chunks, MAX_CHUNKS - 1), CODE_SOURCE_NOTE]
        else:
            chunks = cap_chunks(chunks)
        return _Outcome(
            chunks,
            True,
            None,
            source=source,
            not_found=not_found,
            session_id=r.session_id,
        )
