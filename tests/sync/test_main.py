"""`team-wiki-sync` 진입점(Task 17): 종료 코드·신호·정리 대기·로그 처리기."""

import asyncio
import logging
import signal
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2
import pytest

from team_wiki_engine.config.load import ConfigError
from team_wiki_engine.sync import main as main_module
from team_wiki_engine.sync.main import USAGE, UsageError, amain, engine_dir_from, parse_args
from team_wiki_engine.sync.run import SyncRunError
from tests.helpers.fake_git import write_fake_git
from tests.helpers.sync_env import ENGINE_DIR
from tests.sync.cli_env import API_KEY, make_cli


class DrainCounter:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, timeout: float) -> int:
        self.calls.append(timeout)
        return 0


@pytest.fixture
def drains(monkeypatch: pytest.MonkeyPatch) -> DrainCounter:
    counter = DrainCounter()
    monkeypatch.setattr(main_module, "drain_background_tasks", counter)
    return counter


# ---------------------------------------------------------------- 인자·엔진 폴더


class TestParseArgs:
    def test_없으면_None(self) -> None:
        assert parse_args([]) is None

    @pytest.mark.parametrize("kind", ["code", "ingest"])
    def test_only(self, kind: str) -> None:
        assert parse_args(["--only", kind]) == kind

    @pytest.mark.parametrize(
        "argv",
        [["--only"], ["--only", "x"], ["--help"], ["-h"], ["a", "b"], ["--only=code"],
         ["--only", "code", "x"]],
    )  # fmt: skip
    def test_그_밖은_사용_오류(self, argv: list[str]) -> None:
        with pytest.raises(UsageError):
            parse_args(argv)


class TestEngineDir:
    def test_환경변수를_공백_제거해_쓴다(self, tmp_dir: Path) -> None:
        assert engine_dir_from({"TEAM_WIKI_ENGINE_DIR": f"  {tmp_dir}\n"}) == tmp_dir

    @pytest.mark.parametrize("env", [{}, {"TEAM_WIKI_ENGINE_DIR": "   "}])
    def test_없으면_패키지_위치에서_찾는다(self, env: dict[str, str]) -> None:
        assert engine_dir_from(env) == ENGINE_DIR

    def test_상대_경로는_거부한다(self) -> None:
        with pytest.raises(ConfigError):
            engine_dir_from({"TEAM_WIKI_ENGINE_DIR": "relative/dir"})


# ---------------------------------------------------------------- 종료 코드 2


class TestExit2:
    @pytest.mark.parametrize(
        "argv", [["--only", "x"], ["--help"], ["-h"], ["a", "b"]]
    )  # fmt: skip
    async def test_잘못된_인자(
        self, tmp_dir: Path, capsys: pytest.CaptureFixture[str], argv: list[str]
    ) -> None:
        cli = await make_cli(tmp_dir)
        assert await amain(argv, cli.env, build_deps=cli.fake_build()) == 2
        assert USAGE in capsys.readouterr().err

    async def test_설정_파일이_없다(
        self, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cli = await make_cli(tmp_dir)
        cli.env["TEAM_WIKI_CONFIG"] = str(tmp_dir / "없음.json")
        assert await amain([], cli.env, build_deps=cli.fake_build()) == 2
        assert API_KEY not in capsys.readouterr().err

    async def test_Claude_키가_없다(
        self, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cli = await make_cli(tmp_dir)
        del cli.env["ANTHROPIC_API_KEY"]
        assert await amain([], cli.env, build_deps=cli.fake_build()) == 2
        assert "ANTHROPIC_API_KEY" in capsys.readouterr().err

    async def test_skills_없는_엔진_폴더(
        self, tmp_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cli = await make_cli(tmp_dir)
        empty = tmp_dir / "engine"
        empty.mkdir()
        cli.env["TEAM_WIKI_ENGINE_DIR"] = str(empty)
        assert await amain([], cli.env, build_deps=cli.fake_build()) == 2
        err = capsys.readouterr().err
        assert "skills" in err
        assert str(empty) not in err

    async def test_상대_엔진_폴더(self, tmp_dir: Path) -> None:
        cli = await make_cli(tmp_dir)
        cli.env["TEAM_WIKI_ENGINE_DIR"] = "engine"
        assert await amain([], cli.env, build_deps=cli.fake_build()) == 2

    async def test_낮은_git_버전(self, tmp_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
        cli = await make_cli(tmp_dir)
        fake_bin = tmp_dir / "fakebin"
        write_fake_git(fake_bin, version="git version 2.31.0")
        cli.env["PATH"] = f"{fake_bin}:{cli.env['PATH']}"
        assert await amain([], cli.env, build_deps=cli.fake_build()) == 2
        assert "2.31.0" in capsys.readouterr().err

    async def test_build_deps의_ConfigError는_가려서_알린다(
        self, tmp_dir: Path, capsys: pytest.CaptureFixture[str], drains: DrainCounter
    ) -> None:
        cli = await make_cli(tmp_dir)

        def build(*args: Any, **kwargs: Any) -> Any:
            raise ConfigError(f"나쁜 값 {API_KEY}")

        assert await amain([], cli.env, build_deps=build) == 2
        err = capsys.readouterr().err
        assert "나쁜 값" in err
        assert API_KEY not in err
        assert drains.calls == []  # 의존성이 없으면 정리할 것도 없다


# ---------------------------------------------------------------- 실행 결과


class TestRun:
    async def test_성공은_0(self, tmp_dir: Path, drains: DrainCounter) -> None:
        cli = await make_cli(tmp_dir)
        assert await amain(["--only", "code"], cli.env, build_deps=cli.fake_build()) == 0
        assert len(cli.sync.notifier.sent) == 1
        assert drains.calls == [cli.sync.deps.settle_timeout_s]

    async def test_실패_단계는_1(self, tmp_dir: Path, drains: DrainCounter) -> None:
        cli = await make_cli(tmp_dir)
        cli.sync.runner.set_script(lambda inp, call: {"ok": False, "failure": "timeout"})
        assert await amain(["--only", "code"], cli.env, build_deps=cli.fake_build()) == 1
        assert [mention for _, mention in cli.sync.notifier.sent] == [True]
        assert len(drains.calls) == 1

    async def test_잠금이_바쁘면_0(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch, drains: DrainCounter
    ) -> None:
        cli = await make_cli(tmp_dir)

        async def busy(deps: Any, *, only: Any = None) -> None:
            return None

        monkeypatch.setattr(main_module, "run_once", busy)
        assert await amain([], cli.env, build_deps=cli.fake_build()) == 0
        assert len(drains.calls) == 1

    async def test_run_once_예외는_가려서_알리고_1(
        self,
        tmp_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        drains: DrainCounter,
    ) -> None:
        cli = await make_cli(tmp_dir)
        home = str(cli.sync.paths.home)

        async def boom(deps: Any, *, only: Any = None) -> None:
            raise RuntimeError(f"실패 {API_KEY} {home}/state/x")

        monkeypatch.setattr(main_module, "run_once", boom)
        assert await amain([], cli.env, build_deps=cli.fake_build()) == 1
        [(text, mention)] = cli.sync.notifier.sent
        assert text.startswith("[sync] 실패: ")
        assert mention is True
        err = capsys.readouterr().err
        for shown in (text, err):
            assert API_KEY not in shown
            assert home not in shown
        assert len(drains.calls) == 1

    async def test_SyncRunError_메시지를_그대로_보고한다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch, drains: DrainCounter
    ) -> None:
        cli = await make_cli(tmp_dir)

        async def boom(deps: Any, *, only: Any = None) -> None:
            raise SyncRunError("데이터 사본 준비 실패: 원격 없음")

        monkeypatch.setattr(main_module, "run_once", boom)
        assert await amain([], cli.env, build_deps=cli.fake_build()) == 1
        assert cli.sync.notifier.sent == [("[sync] 실패: 데이터 사본 준비 실패: 원격 없음", True)]

    async def test_정리_대기가_예외여도_종료_코드는_그대로(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli = await make_cli(tmp_dir)

        async def bad_drain(timeout: float) -> int:
            raise RuntimeError("정리 실패")

        monkeypatch.setattr(main_module, "drain_background_tasks", bad_drain)
        assert await amain(["--only", "code"], cli.env, build_deps=cli.fake_build()) == 0

    async def test_정리_대기_중_두_번째_신호는_트레이스백_없이_1로_끝난다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli = await make_cli(tmp_dir)

        async def cancelled_drain(timeout: float) -> int:
            raise asyncio.CancelledError

        monkeypatch.setattr(main_module, "drain_background_tasks", cancelled_drain)
        assert await amain(["--only", "code"], cli.env, build_deps=cli.fake_build()) == 1

    async def test_기본_http_클라이언트_설정(self, tmp_dir: Path) -> None:
        cli = await make_cli(tmp_dir)
        seen: list[httpx2.AsyncClient] = []
        fake = cli.fake_build()

        def build(*args: Any, **kwargs: Any) -> Any:
            seen.append(kwargs["http"])
            return fake(*args, **kwargs)

        assert await amain(["--only", "code"], cli.env, build_deps=build) == 0
        [http] = seen
        assert http.trust_env is False
        assert http.follow_redirects is False
        assert http.timeout == httpx2.Timeout(30.0)
        assert http.is_closed

    async def test_http_팩토리를_쓴다(self, tmp_dir: Path) -> None:
        cli = await make_cli(tmp_dir)
        made: list[httpx2.AsyncClient] = []

        def factory() -> httpx2.AsyncClient:
            client = httpx2.AsyncClient(trust_env=False, follow_redirects=False)
            made.append(client)
            return client

        await amain(["--only", "code"], cli.env, build_deps=cli.fake_build(), http_factory=factory)
        assert len(made) == 1
        assert made[0].is_closed


# ---------------------------------------------------------------- 신호·취소


class _SignalSpy:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.added: dict[int, Callable[[], None]] = {}
        self.removed: list[int] = []
        self._loop = loop
        self._add = loop.add_signal_handler
        self._remove = loop.remove_signal_handler
        loop.add_signal_handler = self.add  # type: ignore[method-assign,assignment]
        loop.remove_signal_handler = self.remove  # type: ignore[method-assign]

    def add(self, sig: int, callback: Callable[..., Any], *args: Any) -> None:
        self.added[sig] = callback
        self._add(sig, callback, *args)

    def remove(self, sig: int) -> bool:
        self.removed.append(sig)
        return self._remove(sig)

    def restore(self) -> None:
        del self._loop.add_signal_handler
        del self._loop.remove_signal_handler


class TestSignals:
    async def test_신호_처리기는_메인_태스크를_취소하고_finally에서_떼어진다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch, drains: DrainCounter
    ) -> None:
        cli = await make_cli(tmp_dir)
        spy = _SignalSpy(asyncio.get_running_loop())
        main_task = asyncio.current_task()
        assert main_task is not None
        reached = asyncio.Event()

        async def hang(deps: Any, *, only: Any = None) -> None:
            assert set(spy.added) == {signal.SIGTERM, signal.SIGINT}
            assert spy.removed == []
            spy.added[signal.SIGTERM]()  # 처리기를 직접 호출
            reached.set()
            await asyncio.sleep(30)

        monkeypatch.setattr(main_module, "run_once", hang)
        try:
            code = await amain([], cli.env, build_deps=cli.fake_build())
        finally:
            spy.restore()
        assert reached.is_set()
        assert code == 1
        assert sorted(spy.removed) == sorted([signal.SIGTERM, signal.SIGINT])
        assert len(drains.calls) == 1
        assert main_task.cancelling() == 0
        # 실제로 떼어졌다: 다시 떼면 False.
        loop = asyncio.get_running_loop()
        assert loop.remove_signal_handler(signal.SIGTERM) is False
        assert loop.remove_signal_handler(signal.SIGINT) is False

    async def test_바깥_취소는_전파하되_정리와_처리기_제거는_한다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch, drains: DrainCounter
    ) -> None:
        cli = await make_cli(tmp_dir)
        started = asyncio.Event()

        async def hang(deps: Any, *, only: Any = None) -> None:
            started.set()
            await asyncio.sleep(30)

        monkeypatch.setattr(main_module, "run_once", hang)
        task = asyncio.create_task(amain([], cli.env, build_deps=cli.fake_build()))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(drains.calls) == 1
        loop = asyncio.get_running_loop()
        assert loop.remove_signal_handler(signal.SIGTERM) is False

    async def test_정상_종료에서도_처리기를_뗀다(self, tmp_dir: Path) -> None:
        cli = await make_cli(tmp_dir)
        await amain(["--only", "code"], cli.env, build_deps=cli.fake_build())
        loop = asyncio.get_running_loop()
        assert loop.remove_signal_handler(signal.SIGTERM) is False
        assert loop.remove_signal_handler(signal.SIGINT) is False


# ---------------------------------------------------------------- 로그·모듈 실행


class TestLoggingAndModule:
    async def test_amain은_루트_로거에_처리기를_붙이지_않는다(self, tmp_dir: Path) -> None:
        cli = await make_cli(tmp_dir)
        root = logging.getLogger()
        before = list(root.handlers)
        await amain(["--only", "code"], cli.env, build_deps=cli.fake_build())
        assert list(root.handlers) == before

    def test_basicConfig는_main에서만_부른다(self) -> None:
        source = Path(main_module.__file__).read_text(encoding="utf-8")
        assert source.count("logging.basicConfig(") == 1
        main_src = source[source.index("def main(") :]
        assert "logging.basicConfig(" in main_src

    def test_python_m은_도움말에_2(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "team_wiki_engine.sync", "--help"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 2
        assert USAGE in result.stderr
