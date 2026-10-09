"""메시지 분배, 관리자 명령, 정지 스위치."""

import asyncio
import json
import logging
import warnings
from datetime import UTC, datetime
from pathlib import Path

import pytest

from team_wiki_engine.bot.dispatch import (
    ADMIN_COMMANDS,
    BotSettings,
    Dispatcher,
    InFlight,
    PauseSwitch,
    StatusSnapshot,
    parse_admin_command,
    should_handle,
    status_text,
)
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
from team_wiki_engine.metrics.record import STATE_FILES
from tests.helpers.bot import BotEnv, FakeReplies, incoming, make_bot_env

SETTINGS = BotSettings(
    guild_id="100", channel_ids=frozenset({"200"}), admin_user_id="400", ops_channel_id="300"
)
BOT = "999"
SECRET = "ghp_" + "Qq1Ww2Ee3Rr4Tt5Yy6Uu7Ii8Oo9Pp0Aa1Ss2"


class TestShouldHandle:
    def test_allowed_channel_and_mention_only(self) -> None:
        assert should_handle(incoming(), SETTINGS, BOT)
        assert not should_handle(incoming(content="멘션 없음"), SETTINGS, BOT)

    def test_thread_under_allowed_channel(self) -> None:
        m = incoming(channel_id="900", parent_channel_id="200")
        assert should_handle(m, SETTINGS, BOT)
        assert not should_handle(incoming(channel_id="900", parent_channel_id="201"), SETTINGS, BOT)

    def test_thread_without_mention_ignored(self) -> None:
        m = incoming(channel_id="900", parent_channel_id="200", content="그냥 말")
        assert not should_handle(m, SETTINGS, BOT)

    @pytest.mark.parametrize(
        "override",
        [
            {"author_is_bot": True},
            {"is_system": True},
            {"guild_id": None},
            {"guild_id": "101"},
            {"channel_id": "201"},
            {"content": "@everyone 질문"},
            {"content": "<@&999> 질문"},
            {"message_id": "x"},
            {"author_id": "5\n"},
            {"channel_id": "2\n00"},
        ],
    )
    def test_rejected_inputs(self, override: dict[str, object]) -> None:
        assert not should_handle(incoming(**override), SETTINGS, BOT)


def test_parse_admin_command() -> None:
    assert dict(ADMIN_COMMANDS) == {"!정지": "pause", "!재개": "resume", "!상태": "status"}
    assert parse_admin_command("!정지") == "pause"
    for text in ("!정지 해줘", " !정지", "！정지", "", "!정지\n"):
        assert parse_admin_command(text) is None


def make_dispatcher(
    env: BotEnv, *, settings: BotSettings = SETTINGS, max_tasks: int = 10
) -> tuple[Dispatcher, InFlight, PauseSwitch]:
    pause = PauseSwitch(env.paths.state_dir / STATE_FILES.bot_control, now=env.clock.now)
    in_flight = InFlight(max_tasks)
    dispatcher = Dispatcher(
        settings=settings,
        pipeline=env.pipeline,
        limiter=env.limiter,
        pause=pause,
        in_flight=in_flight,
        refresh_state=lambda: RefreshState(
            last_ok_at=datetime(2026, 10, 7, 1, 0, tzinfo=UTC), consecutive_failures=2
        ),
        notifier=env.notifier,
        user_salt="100",
    )
    return dispatcher, in_flight, pause


async def test_normal_question(tmp_dir: Path) -> None:
    async with asyncio.timeout(10):
        env = await make_bot_env(tmp_dir)
        d, flight, _ = make_dispatcher(env)
        await d.on_ready(BOT)
        replies = FakeReplies()
        await d.on_message(incoming(), replies)
        await flight.wait(5)
        assert len(replies.thread.posts) == 1
        assert len(env.runner.inputs) == 1


async def test_ignored_before_ready_and_after_close(tmp_dir: Path) -> None:
    async with asyncio.timeout(10):
        env = await make_bot_env(tmp_dir)
        d, flight, _ = make_dispatcher(env)
        await d.on_message(incoming(), FakeReplies())
        await d.on_ready(BOT)
        d.close()
        assert flight.closed
        await d.on_message(incoming(message_id="1001"), FakeReplies())
        await flight.wait(1)
        assert env.runner.inputs == []


async def test_duplicate_message_id_answers_once(tmp_dir: Path) -> None:
    async with asyncio.timeout(10):
        env = await make_bot_env(tmp_dir)
        d, flight, _ = make_dispatcher(env)
        await d.on_ready(BOT)
        for _ in range(2):
            await d.on_message(incoming(), FakeReplies())
        await flight.wait(5)
        assert len(env.runner.inputs) == 1


async def test_unrelated_traffic_does_not_evict_dedupe(tmp_dir: Path) -> None:
    async with asyncio.timeout(10):
        env = await make_bot_env(tmp_dir)
        d, flight, _ = make_dispatcher(env)
        await d.on_ready(BOT)
        await d.on_message(incoming(), FakeReplies())
        for i in range(1100):
            await d.on_message(incoming(message_id=str(2000 + i), content="잡담"), FakeReplies())
        await d.on_message(incoming(), FakeReplies())
        await flight.wait(5)
        assert len(env.runner.inputs) == 1


async def test_too_long_question(tmp_dir: Path) -> None:
    async with asyncio.timeout(10):
        env = await make_bot_env(tmp_dir)
        d, flight, _ = make_dispatcher(env)
        await d.on_ready(BOT)
        replies = FakeReplies()
        await d.on_message(incoming(content="<@999> " + "가" * 4001), replies)
        await flight.wait(5)
        assert replies.replies == [TOO_LONG_TEXT]
        assert env.runner.inputs == [] and env.limiter.daily_used() == 0


@pytest.mark.parametrize("attach", [False, True])
async def test_usage_text_for_empty_question(tmp_dir: Path, attach: bool) -> None:
    async with asyncio.timeout(10):
        env = await make_bot_env(tmp_dir)
        d, flight, _ = make_dispatcher(env)
        await d.on_ready(BOT)
        replies = FakeReplies()
        await d.on_message(incoming(content="<@999>", has_attachments=attach), replies)
        await flight.wait(5)
        assert replies.replies == [USAGE_TEXT]
        assert env.runner.inputs == []


async def test_pause_resume_flow(tmp_dir: Path) -> None:
    async with asyncio.timeout(10):
        env = await make_bot_env(tmp_dir, limits={"perUserPerMinute": 20})
        d, flight, _ = make_dispatcher(env)
        await d.on_ready(BOT)
        admin = FakeReplies()
        await d.on_message(incoming(author_id="400", content="<@999> !정지"), admin)
        await flight.wait(5)
        assert admin.replies == [PAUSE_ACK_TEXT]
        state = json.loads((env.paths.state_dir / STATE_FILES.bot_control).read_text())
        assert state["paused"] is True and "400" not in state["changed_by"]
        assert env.notifier.sent == [("[bot] 관리자가 답변을 멈췄습니다", False)]
        user = FakeReplies()
        await d.on_message(incoming(message_id="1001"), user)
        await flight.wait(5)
        assert user.replies == [PAUSED_TEXT] and env.runner.inputs == []
        restarted = PauseSwitch(env.paths.state_dir / STATE_FILES.bot_control, now=env.clock.now)
        assert restarted.paused is True
        await d.on_message(
            incoming(message_id="1002", author_id="400", content="<@999> !재개"), admin
        )
        await flight.wait(5)
        assert admin.replies[-1] == RESUME_ACK_TEXT
        again = FakeReplies()
        await d.on_message(incoming(message_id="1003"), again)
        await flight.wait(5)
        assert len(again.thread.posts) == 1


@pytest.mark.parametrize("admin_user_id", ["400", None])
async def test_non_admin_command(tmp_dir: Path, admin_user_id: str | None) -> None:
    async with asyncio.timeout(10):
        env = await make_bot_env(tmp_dir)
        settings = BotSettings(SETTINGS.guild_id, SETTINGS.channel_ids, admin_user_id, None)
        d, flight, pause = make_dispatcher(env, settings=settings)
        await d.on_ready(BOT)
        replies = FakeReplies()
        await d.on_message(incoming(content="<@999> !정지"), replies)
        await flight.wait(5)
        assert replies.replies == [ADMIN_ONLY_TEXT]
        assert not pause.paused and env.runner.inputs == [] and env.notifier.sent == []


def test_status_text_format() -> None:
    snap = StatusSnapshot(
        paused=False,
        in_flight=1,
        daily_used=3,
        daily_total=60,
        cost_today=0.5,
        refresh=RefreshState(
            last_ok_at=datetime(2026, 10, 7, 1, 0, tzinfo=UTC), consecutive_failures=2
        ),
    )
    assert status_text(snap).split("\n") == [
        "상태: 답변 중",
        "진행 중 질문: 1",
        "오늘 질문: 3 / 60",
        "오늘 비용: $0.50",
        "위키 사본 갱신: 2026-10-07 10:00 성공 · 이후 실패 2회",
    ]
    never = StatusSnapshot(True, 0, 0, 60, 0.0, RefreshState())
    assert status_text(never).split("\n")[0] == "상태: 멈춤"
    assert status_text(never).endswith("위키 사본 갱신: 성공 기록 없음")


async def test_status_command(tmp_dir: Path) -> None:
    async with asyncio.timeout(10):
        env = await make_bot_env(tmp_dir)
        d, flight, _ = make_dispatcher(env)
        await d.on_ready(BOT)
        replies = FakeReplies()
        await d.on_message(incoming(author_id="400", content="<@999> !상태"), replies)
        await flight.wait(5)
        assert "오늘 질문: 0 / 60" in replies.replies[0]
        assert "위키 사본 갱신: 2026-10-07 10:00 성공 · 이후 실패 2회" in replies.replies[0]


@pytest.mark.parametrize("content", [b"garbage{{", b"[]", b"x" * 2_000_000])
def test_bad_control_file_does_not_pause(tmp_dir: Path, content: bytes) -> None:
    file = tmp_dir / "bot-control.json"
    file.write_bytes(content)
    clock_now = lambda: datetime(2026, 10, 7, tzinfo=UTC)  # noqa: E731
    assert PauseSwitch(file, now=clock_now).paused is False


async def test_same_thread_second_question_is_busy(tmp_dir: Path) -> None:
    async with asyncio.timeout(10):
        env = await make_bot_env(tmp_dir, limits={"perUserPerMinute": 10})
        d, flight, _ = make_dispatcher(env)
        await d.on_ready(BOT)
        first, second = FakeReplies(), FakeReplies()
        thread = {"channel_id": "900", "parent_channel_id": "200"}
        await d.on_message(incoming(**thread), first)
        await d.on_message(incoming(message_id="1001", **thread), second)
        await flight.wait(5)
        assert second.replies == [rejection_text("user_busy")]
        assert len(env.runner.inputs) == 1 and env.limiter.daily_used() == 1
        assert env.pipeline.thread_busy("900") is False


async def test_reply_failure_is_contained(tmp_dir: Path, caplog: pytest.LogCaptureFixture) -> None:
    async with asyncio.timeout(10):
        env = await make_bot_env(tmp_dir)
        d, flight, _ = make_dispatcher(env)
        await d.on_ready(BOT)
        with caplog.at_level(logging.DEBUG):
            await d.on_message(
                incoming(content="<@999>"), FakeReplies(reply_error=RuntimeError(SECRET))
            )
            await flight.wait(5)
        assert SECRET not in caplog.text and "RuntimeError" in caplog.text


async def test_in_flight_limits() -> None:
    async with asyncio.timeout(10):
        gate = asyncio.Event()

        async def work() -> None:
            await gate.wait()

        flight = InFlight(1)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            assert flight.spawn(work(), name="a") is True
            refused = work()
            assert flight.spawn(refused, name="b") is False
            import gc

            gc.collect()
        assert not [w for w in caught if issubclass(w.category, RuntimeWarning)]
        assert len(flight) == 1
        assert await flight.wait(0.05) == 1
        assert len(flight) == 1  # 취소하지 않았다
        flight.cancel_all()
        assert await flight.wait(1) == 0
        flight.close()
        late = work()
        assert flight.spawn(late, name="c") is False and flight.closed
        gate.set()
