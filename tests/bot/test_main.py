"""`team-wiki-bot` 진입점: 종료 코드, data.lock, 신호, 로그 처리기."""

import asyncio
import json
import logging
import os
import signal
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2
import pytest

from team_wiki_engine.bot import main as main_module
from team_wiki_engine.bot.main import USAGE, acquire_data_lock, amain
from team_wiki_engine.bot.ports import Gateway, GatewayConfigError
from team_wiki_engine.bot.wiring import build_bot_deps
from team_wiki_engine.config.load import ConfigError
from team_wiki_engine.metrics.record import STATE_FILES
from team_wiki_engine.sync.lock import acquire_lock
from tests.helpers.bot import (
    BotEnv,
    FakeGateway,
    FakeReplies,
    incoming,
    make_bot_env,
    wait_until,
)
from tests.helpers.fake_git import write_fake_git
from tests.helpers.http import mock_client
from tests.helpers.runner import ScriptedRunner

API_KEY = "sk-ant-api03-SECRETKEYVALUE0123456789"
# 가짜 값. 한 문자열로 쓰면 GitHub push 보호가 진짜 디스코드 토큰으로 보고 push를 막는다.
TOKEN = ".".join(["MTIzNDU2Nzg5MDEyMzQ1Njc4", "GhIjKl", "AbCdEfGhIjKlMnOpQrStUvWxYz012345"])


class Setup:
    def __init__(self, env: BotEnv) -> None:
        self.env = env
        self.gateway = FakeGateway()
        self.runner = ScriptedRunner()
        cfg = {
            "repos": [{"name": "worker", "url": "https://github.com/o/worker.git"}],
            "dataRepo": {"url": env.config.data_repo.url},
            "discord": {
                "guildId": "100",
                "channelIds": ["200"],
                "opsChannelId": "300",
                "adminUserId": "400",
            },
        }
        self.config_path = env.root / "team.json"
        self.write_config(cfg)
        self.vars: dict[str, str] = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(env.root / "git-home"),
            "TEAM_WIKI_HOME": str(env.paths.home),
            "TEAM_WIKI_CONFIG": str(self.config_path),
            "TEAM_WIKI_ENGINE_DIR": str(env.paths.engine_dir),
            "ANTHROPIC_API_KEY": API_KEY,
            "DISCORD_TOKEN": TOKEN,
        }

    def write_config(self, cfg: dict[str, Any]) -> None:
        self.config_path.write_text(json.dumps(cfg), encoding="utf-8")

    def http(self) -> httpx2.AsyncClient:
        return mock_client(lambda request: httpx2.Response(204))

    def factory(self) -> Callable[[str], Gateway]:
        return lambda token: self.gateway

    def build(self) -> Callable[..., Any]:
        runner = self.runner

        def build(*args: Any, **kwargs: Any) -> Any:
            return build_bot_deps(*args, runner=runner, **kwargs)

        return build

    async def run(
        self, argv: list[str] | None = None, stop: asyncio.Event | None = None, **kw: Any
    ) -> int:
        return await amain(
            argv or [],
            self.vars,
            gateway_factory=self.factory(),
            http_factory=self.http,
            build_deps=kw.pop("build_deps", self.build()),
            stop=stop,
            **kw,
        )


async def setup(tmp_dir: Path) -> Setup:
    return Setup(await make_bot_env(tmp_dir))


@pytest.mark.parametrize("argv", [["--help"], ["x"]])
async def test_usage_errors(
    tmp_dir: Path, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    async with asyncio.timeout(20):
        s = await setup(tmp_dir)
        assert await s.run(argv) == 2
        assert USAGE in capsys.readouterr().err


async def test_config_errors_exit_2(tmp_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    async with asyncio.timeout(20):
        s = await setup(tmp_dir)
        s.vars["TEAM_WIKI_CONFIG"] = str(tmp_dir / "없음.json")
        assert await s.run() == 2
        s.vars["TEAM_WIKI_CONFIG"] = str(s.config_path)
        del s.vars["DISCORD_TOKEN"]
        assert await s.run() == 2
        s.vars["DISCORD_TOKEN"] = TOKEN
        empty = tmp_dir / "engine"
        empty.mkdir()
        s.vars["TEAM_WIKI_ENGINE_DIR"] = str(empty)
        assert await s.run() == 2
        s.vars["TEAM_WIKI_ENGINE_DIR"] = str(s.env.paths.engine_dir)
        write_fake_git(tmp_dir / "fakebin", version="git version 2.31.0")
        saved = s.vars["PATH"]
        s.vars["PATH"] = f"{tmp_dir / 'fakebin'}:{saved}"
        assert await s.run() == 2
        s.vars["PATH"] = saved
        s.write_config({**json.loads(s.config_path.read_text()), "discord": {}})
        assert await s.run() == 2
        err = capsys.readouterr().err
        assert API_KEY not in err and TOKEN not in err


async def test_build_deps_config_error_exit_2(
    tmp_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    async with asyncio.timeout(20):
        s = await setup(tmp_dir)

        def boom(*args: Any, **kwargs: Any) -> Any:
            raise ConfigError("나쁜 값 " + API_KEY)

        assert await s.run(build_deps=boom) == 2
        err = capsys.readouterr().err
        assert "나쁜 값" in err and API_KEY not in err
        # 잠금은 풀렸다
        lock = acquire_lock(s.env.paths.state_dir / STATE_FILES.data_lock, now=s.env.clock.now())
        assert lock is not None
        lock.release()


async def test_data_lock_held_exits_1(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async with asyncio.timeout(20):
        s = await setup(tmp_dir)
        monkeypatch.setattr(main_module, "DATA_LOCK_WAIT_S", 0.2)
        held = acquire_lock(s.env.paths.state_dir / STATE_FILES.data_lock, now=s.env.clock.now())
        assert held is not None
        try:
            assert await s.run() == 1
        finally:
            held.release()
        err = capsys.readouterr().err
        assert "data 사본" in err and str(s.env.paths.home) not in err


async def test_acquire_data_lock_none_after_wait(tmp_dir: Path) -> None:
    async with asyncio.timeout(20):
        s = await setup(tmp_dir)
        state = s.env.paths.state_dir
        held = acquire_lock(state / STATE_FILES.data_lock, now=s.env.clock.now())
        assert held is not None
        assert await acquire_data_lock(state, now=s.env.clock.now, wait_s=0.1) is None
        held.release()
        again = await acquire_data_lock(state, now=s.env.clock.now, wait_s=0.1)
        assert again is not None
        again.release()


async def test_normal_run_releases_lock(tmp_dir: Path) -> None:
    async with asyncio.timeout(20):
        s = await setup(tmp_dir)
        stop = asyncio.Event()
        task = asyncio.create_task(s.run(stop=stop))
        await s.gateway.ready.wait()
        replies = FakeReplies()
        await s.gateway.deliver(incoming(), replies)
        await wait_until(lambda: bool(replies.thread.posts))
        stop.set()
        assert await task == 0
        lock = acquire_lock(s.env.paths.state_dir / STATE_FILES.data_lock, now=s.env.clock.now())
        assert lock is not None
        lock.release()


async def test_sigterm_handler_stops_and_is_removed(tmp_dir: Path) -> None:
    async with asyncio.timeout(20):
        s = await setup(tmp_dir)
        loop = asyncio.get_running_loop()
        task = asyncio.create_task(s.run())
        await s.gateway.ready.wait()
        handlers = loop._signal_handlers  # type: ignore[attr-defined]
        assert signal.SIGTERM in handlers and signal.SIGINT in handlers
        handlers[signal.SIGTERM]._run()
        assert await task == 0
        assert loop.remove_signal_handler(signal.SIGTERM) is False
        assert loop.remove_signal_handler(signal.SIGINT) is False


async def test_gateway_config_error_exits_2(tmp_dir: Path) -> None:
    async with asyncio.timeout(20):
        s = await setup(tmp_dir)
        s.gateway = FakeGateway(fail=GatewayConfigError("Intent를 켜야 합니다"))
        assert await s.run() == 2


async def test_no_root_logger_handlers_added(tmp_dir: Path) -> None:
    async with asyncio.timeout(20):
        s = await setup(tmp_dir)
        before = list(logging.getLogger().handlers)
        s.gateway = FakeGateway(return_early=True)
        await s.run()
        assert logging.getLogger().handlers == before
