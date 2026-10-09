"""봇 수명 주기: 시작, 정해진 종료 순서, 종료 코드, drain 한 번."""

import asyncio
import shutil
import socket
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from team_wiki_engine.bot import app as app_module
from team_wiki_engine.bot.app import BotApp, BotDeps
from team_wiki_engine.bot.ports import GatewayConfigError
from team_wiki_engine.bot.refresh import DataRefresher
from team_wiki_engine.bot.replies import SHUTDOWN_TEXT
from team_wiki_engine.claude.types import RunClaudeInput, RunClaudeResult
from team_wiki_engine.sync.stage import Mask
from team_wiki_engine.util.aio import run_to_completion
from tests.bot.test_dispatch import make_dispatcher
from tests.helpers.bot import (
    BotEnv,
    FakeGateway,
    FakeReplies,
    incoming,
    make_bot_env,
    wait_until,
)
from tests.helpers.hang_guard import hang_guard
from tests.helpers.runner import Drain as FakeDrain
from tests.helpers.runner import ok_result

ANTHROPIC_KEY = "sk-ant-api03-" + "k" * 30


class Parts:
    def __init__(self) -> None:
        self.order: list[str] = []
        self.refresh_calls = 0


async def build(
    tmp_dir: Path,
    *,
    runner: Callable[[RunClaudeInput], object] | None = None,
    gateway: FakeGateway | None = None,
    refresh: Callable[[], object] | None = None,
    env_vars: dict[str, str] | None = None,
    interval_s: float = 600.0,
    drain: FakeDrain | None = None,
) -> tuple[BotApp, BotEnv, FakeGateway, FakeDrain, BotDeps, Parts]:
    parts = Parts()
    env = await make_bot_env(tmp_dir, runner=runner)  # type: ignore[arg-type]
    gw = gateway or FakeGateway()
    dispatcher, in_flight, _ = make_dispatcher(env)

    async def default_refresh() -> None:
        parts.refresh_calls += 1
        gw.events.append("refresh")

    refresher = DataRefresher(
        refresh=refresh or default_refresh,  # type: ignore[arg-type]
        gate=env.gate,
        data_dir=env.paths.data_dir,
        notifier=env.notifier,
        mask=Mask((), {}),
        now=env.clock.now,
        interval_s=interval_s,
    )
    the_drain = drain or FakeDrain()
    deps = BotDeps(
        pipeline=env.pipeline,
        dispatcher=dispatcher,
        refresher=refresher,
        in_flight=in_flight,
        notifier=env.notifier,
        gateway=gw,
        env=env_vars or {},
        drain=the_drain,
    )
    return BotApp(deps), env, gw, the_drain, deps, parts


@pytest.fixture
def sock_dir() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def received(server: socket.socket) -> list[bytes]:
    out: list[bytes] = []
    while True:
        try:
            out.append(server.recv(100))
        except BlockingIOError:
            return out


class Waiting:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, inp: RunClaudeInput) -> RunClaudeResult:
        self.started.set()
        await self.release.wait()
        return ok_result()


async def test_normal_run_order_and_ready(tmp_dir: Path, sock_dir: Path) -> None:
    async with asyncio.timeout(10):
        path = sock_dir / "n.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        server.bind(str(path))
        server.setblocking(False)
        try:
            app, env, gw, drain, deps, _ = await build(
                tmp_dir, env_vars={"NOTIFY_SOCKET": str(path)}
            )
            seen: list[str] = []

            async def spy(timeout: float) -> int:
                seen.append(f"closed={deps.in_flight.closed},gw_closed={gw.closed}")
                seen.append(f"sent={[t for t, _ in env.notifier.sent]}")
                return await drain(timeout)

            object.__setattr__(deps, "drain", spy)
            stop = asyncio.Event()
            task = asyncio.create_task(app.run(stop))
            await gw.ready.wait()
            assert gw.events[:2] == ["refresh", "ready"]
            replies = FakeReplies()
            await gw.deliver(incoming(), replies)
            await wait_until(lambda: bool(replies.thread.posts))
            stop.set()
            assert await task == 0
            assert drain.calls == [30.0]
            assert seen[0] == "closed=True,gw_closed=False"
            assert "종료합니다" not in seen[1]
            assert env.notifier.sent[-1][0] == "[bot] 종료합니다"
            assert gw.closed
            assert received(server) == [b"READY=1", b"STOPPING=1"]
        finally:
            server.close()


async def test_pending_answer_cancelled_after_grace_before_drain(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with asyncio.timeout(10):
        monkeypatch.setattr(app_module, "SHUTDOWN_GRACE_S", 0.1)
        waiting = Waiting()
        app, env, gw, drain, deps, _ = await build(tmp_dir, runner=waiting)
        thread = FakeReplies()
        posts_at_drain: list[int] = []

        async def spy(timeout: float) -> int:
            posts_at_drain.append(len(thread.thread.posts))
            return await drain(timeout)

        object.__setattr__(deps, "drain", spy)
        stop = asyncio.Event()
        task = asyncio.create_task(app.run(stop))
        await gw.ready.wait()
        await gw.deliver(incoming(), thread)
        await waiting.started.wait()
        stop.set()
        assert await task == 0
        assert thread.thread.posts == [[SHUTDOWN_TEXT]]
        assert posts_at_drain == [1]
        assert env.limiter.active() == 0


async def test_answer_finishing_within_grace_is_not_cancelled(tmp_dir: Path) -> None:
    async with asyncio.timeout(10):
        started = asyncio.Event()

        async def slow(inp: RunClaudeInput) -> RunClaudeResult:
            started.set()
            await asyncio.sleep(0.1)
            return ok_result(text="정상 답")

        app, env, gw, drain, deps, _ = await build(tmp_dir, runner=slow)
        replies = FakeReplies()
        stop = asyncio.Event()
        task = asyncio.create_task(app.run(stop))
        await gw.ready.wait()
        await gw.deliver(incoming(), replies)
        await started.wait()
        stop.set()
        assert await task == 0
        assert replies.thread.posts == [["정상 답"]]


@pytest.mark.parametrize(
    ("kwargs", "code", "notice"),
    [
        (
            {"fail": GatewayConfigError("Message Content Intent를 켜야 합니다")},
            2,
            ("[bot] 디스코드 설정 오류: Message Content Intent를 켜야 합니다", True),
        ),
        (
            {"fail": RuntimeError("비밀 ghp_x")},
            1,
            ("[bot] 디스코드 연결이 끝났습니다(RuntimeError)", True),
        ),
        ({"return_early": True}, 1, None),
    ],
)
async def test_gateway_end_codes(
    tmp_dir: Path, kwargs: dict[str, object], code: int, notice: tuple[str, bool] | None
) -> None:
    async with asyncio.timeout(10):
        gw = FakeGateway(**kwargs)  # type: ignore[arg-type]
        app, env, _, drain, _, _ = await build(tmp_dir, gateway=gw)
        assert await app.run(asyncio.Event()) == code
        assert drain.calls == [30.0]
        assert gw.closed
        if notice is not None:
            assert notice in env.notifier.sent
        assert all("ghp_x" not in text for text, _ in env.notifier.sent)
        assert any(mention for _, mention in env.notifier.sent)


async def test_outer_cancel_still_drains_once(tmp_dir: Path) -> None:
    async with asyncio.timeout(10):
        app, env, gw, drain, _, _ = await build(tmp_dir)
        task = asyncio.create_task(app.run(asyncio.Event()))
        await gw.ready.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert drain.calls == [30.0]
        assert gw.closed


async def test_refresh_ignoring_cancel_does_not_block_shutdown(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "REFRESH_STOP_WAIT_S", 0.1)
    calls = 0
    finished = asyncio.Event()

    async def stubborn() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            return
        try:
            await run_to_completion(asyncio.sleep(1.0))
        finally:
            finished.set()

    app, env, gw, drain, _, _ = await build(tmp_dir, refresh=stubborn, interval_s=0.01)
    stop = asyncio.Event()
    with hang_guard(5):
        task = asyncio.create_task(app.run(stop))
        await gw.ready.wait()
        await wait_until(lambda: calls >= 2)
        stop.set()
        assert await task == 0
    assert drain.calls == [30.0]
    await asyncio.wait_for(finished.wait(), 5)


async def test_ready_notice_once_across_reconnects(tmp_dir: Path, sock_dir: Path) -> None:
    async with asyncio.timeout(10):
        path = sock_dir / "n.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        server.bind(str(path))
        server.setblocking(False)
        try:
            app, env, gw, _, _, _ = await build(tmp_dir, env_vars={"NOTIFY_SOCKET": str(path)})
            stop = asyncio.Event()
            task = asyncio.create_task(app.run(stop))
            await gw.ready.wait()
            await gw.reconnect()
            stop.set()
            await task
            assert received(server).count(b"READY=1") == 1
            assert [t for t, _ in env.notifier.sent].count("[bot] 디스코드에 연결했습니다") == 1
        finally:
            server.close()


async def test_deps_repr_hides_env(tmp_dir: Path) -> None:
    async with asyncio.timeout(10):
        _, _, _, _, deps, _ = await build(tmp_dir, env_vars={"ANTHROPIC_API_KEY": ANTHROPIC_KEY})
        assert ANTHROPIC_KEY not in repr(deps)
