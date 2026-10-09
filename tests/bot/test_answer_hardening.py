"""질문 처리 흐름 보강: 취소 안내, 거절 간격, 운영 알림, 스레드 점유, 쓰기 실패."""

import asyncio
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from team_wiki_engine.bot import answer
from team_wiki_engine.bot.replies import DATA_BUSY_TEXT, SHUTDOWN_TEXT, rejection_text
from team_wiki_engine.claude.types import RunClaudeInput, RunClaudeResult
from team_wiki_engine.metrics.record import QuestionRecord
from tests.helpers.bot import USER_ID, FakeQuestion, FakeThread, make_bot_env
from tests.helpers.hang_guard import hang_guard
from tests.helpers.runner import ok_result

SECRET = "ghp_" + "Zz9Yy8Xx7Ww6Vv5Uu4Tt3Ss2Rr1Qq0Pp9Oo8"


class WaitingRunner:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, inp: RunClaudeInput) -> RunClaudeResult:
        self.started.set()
        await self.release.wait()
        return ok_result()


class HangingThread(FakeThread):
    async def post(self, chunks: Any) -> None:
        await asyncio.Event().wait()


async def test_data_wait_limit_then_recovers(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with asyncio.timeout(10):
        monkeypatch.setattr(answer, "DATA_WAIT_S", 0.05)
        env = await make_bot_env(tmp_dir, limits={"perUserPerMinute": 10})
        ctx = FakeQuestion("질문")
        async with env.gate.writing():
            assert await env.pipeline.handle(ctx) == "failed"
        assert ctx.thread.posts == [[DATA_BUSY_TEXT]]
        assert env.runner.inputs == []
        assert env.gate.readers == 0
        ok = FakeQuestion("다음")
        assert await env.pipeline.handle(ok) == "answered"


async def test_cancel_notifies_records_and_releases(tmp_dir: Path) -> None:
    async with asyncio.timeout(10):
        waiting = WaitingRunner()
        env = await make_bot_env(tmp_dir, runner=waiting)
        ctx = FakeQuestion("질문")
        task = asyncio.create_task(env.pipeline.handle(ctx))
        await waiting.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert ctx.thread.posts == [[SHUTDOWN_TEXT]]
        (rec,) = env.questions()
        assert rec.failure == "cancelled" and rec.ok is False
        assert env.limiter.active() == 0
        assert env.pipeline.thread_busy("900") is False
        assert env.gate.readers == 0
        assert env.sessions.get("900") is None


async def test_cancel_notice_is_bounded(tmp_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    waiting = WaitingRunner()
    monkeypatch.setattr(answer, "SHUTDOWN_NOTICE_TIMEOUT_S", 0.1)
    env = await make_bot_env(tmp_dir, runner=waiting)
    ctx = FakeQuestion("질문", thread=HangingThread())
    with hang_guard(5):
        task = asyncio.create_task(env.pipeline.handle(ctx))
        await waiting.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert env.questions()[0].failure == "cancelled"
    assert env.limiter.active() == 0


async def test_post_failure_is_recorded_without_session(
    tmp_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async with asyncio.timeout(10):
        env = await make_bot_env(tmp_dir)
        ctx = FakeQuestion("질문", thread=FakeThread(fail_post=RuntimeError("…" + SECRET)))
        with caplog.at_level(logging.DEBUG):
            assert await env.pipeline.handle(ctx) == "failed"
        (rec,) = env.questions()
        assert rec.failure == "post" and rec.ok is False
        assert env.sessions.get("900") is None
        assert SECRET not in caplog.text and "RuntimeError" in caplog.text


async def test_session_save_failure_is_logged_only(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with asyncio.timeout(10):
        env = await make_bot_env(tmp_dir)

        def boom(thread_id: str, session_id: str) -> None:
            raise OSError("disk")

        monkeypatch.setattr(env.sessions, "set", boom)
        assert await env.pipeline.handle(FakeQuestion("질문")) == "answered"
        assert env.questions()[0].ok is True


async def test_record_write_failure_is_logged_only(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with asyncio.timeout(10):
        env = await make_bot_env(tmp_dir)

        def boom(*args: Any, **kwargs: Any) -> None:
            raise OSError("disk")

        monkeypatch.setattr(answer, "append_jsonl", boom)
        ctx = FakeQuestion("질문")
        assert await env.pipeline.handle(ctx) == "answered"
        assert len(ctx.thread.posts) == 1


async def test_reject_reply_cooldown(tmp_dir: Path) -> None:
    async with asyncio.timeout(10):
        env = await make_bot_env(tmp_dir, limits={"perUserPerMinute": 1})
        assert await env.pipeline.handle(FakeQuestion("하나")) == "answered"
        asked = [FakeQuestion("둘") for _ in range(3)]
        for q in asked:
            assert await env.pipeline.handle(q) == "rejected"
            env.clock.advance(5)
        assert sum(len(q.replies) for q in asked) == 1
        env.clock.advance(31)
        later = FakeQuestion("셋")
        assert await env.pipeline.handle(later) == "rejected"
        assert later.replies == [rejection_text("user_rate")]


async def test_daily_quota_notice_once_per_day(tmp_dir: Path) -> None:
    async with asyncio.timeout(10):
        env = await make_bot_env(tmp_dir, limits={"dailyTotal": 1, "perUserPerMinute": 50})
        assert await env.pipeline.handle(FakeQuestion("하나")) == "answered"
        assert await env.pipeline.handle(FakeQuestion("둘", user_id="601")) == "rejected"
        assert await env.pipeline.handle(FakeQuestion("셋", user_id="602")) == "rejected"
        assert env.notifier.sent == [("[bot] 오늘 질문 한도(1회)에 닿았습니다", False)]
        env.clock.advance(86400)
        assert await env.pipeline.handle(FakeQuestion("넷")) == "answered"
        assert await env.pipeline.handle(FakeQuestion("다섯", user_id="603")) == "rejected"
        assert len(env.notifier.sent) == 2


async def test_thread_claim(tmp_dir: Path) -> None:
    async with asyncio.timeout(10):
        env = await make_bot_env(tmp_dir)
        pipe = env.pipeline
        assert pipe.claim_thread("900") is True
        assert pipe.thread_busy("900") is True
        assert pipe.claim_thread("900") is False
        assert await pipe.handle(FakeQuestion("질문", existing_thread_id="900")) == "answered"
        assert pipe.thread_busy("900") is False
        # 거절로 끝나도 놓인다
        env2 = await make_bot_env(tmp_dir / "b", limits={"perUserPerMinute": 1})
        assert await env2.pipeline.handle(FakeQuestion("하나")) == "answered"
        assert env2.pipeline.claim_thread("901")
        rejected = FakeQuestion("둘", existing_thread_id="901")
        assert await env2.pipeline.handle(rejected) == "rejected"
        assert env2.pipeline.thread_busy("901") is False


async def test_unexpected_error_notice_cooldown(
    tmp_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async with asyncio.timeout(10):

        async def boom(inp: RunClaudeInput) -> RunClaudeResult:
            raise RuntimeError("비밀 " + SECRET)

        env = await make_bot_env(tmp_dir, runner=boom)
        first = FakeQuestion("비밀질문")
        with caplog.at_level(logging.DEBUG):
            assert await env.pipeline.handle(first) == "failed"
            assert await env.pipeline.handle(FakeQuestion("둘", user_id="601")) == "failed"
        assert env.notifier.sent == [("[bot] 질문 처리 중 오류: RuntimeError", False)]
        assert first.thread.posts and "답을 만들지 못했어요" in first.thread.posts[0][0]
        assert env.limiter.active() == 0 and env.gate.readers == 0
        for needle in (SECRET, "비밀질문", USER_ID):
            assert needle not in caplog.text
        env.clock.advance(601)
        assert await env.pipeline.handle(FakeQuestion("셋", user_id="602")) == "failed"
        assert len(env.notifier.sent) == 2


async def test_duration_cost_today(tmp_dir: Path) -> None:
    async with asyncio.timeout(10):
        env = await make_bot_env(tmp_dir, limits={"perUserPerMinute": 10})
        wall = env.clock.now()

        def script(inp: RunClaudeInput, call: int) -> Mapping[str, Any]:
            env.clock.advance(2)
            env.clock.set_wall(wall.replace(year=2020))
            return {}

        env.runner.set_script(script)
        assert await env.pipeline.handle(FakeQuestion("질문")) == "answered"
        (rec,) = env.questions()
        assert rec.duration_ms == 2000
        assert isinstance(rec, QuestionRecord)
        assert env.pipeline.cost_today() == pytest.approx(0.01)
        env.clock.set_wall(wall.replace(day=9))
        assert env.pipeline.cost_today() == 0.0
        env.pipeline.seed_cost_today(0.5)
        env.runner.set_script(lambda inp, call: {})
        assert await env.pipeline.handle(FakeQuestion("또")) == "answered"
        assert env.pipeline.cost_today() == pytest.approx(0.51)
