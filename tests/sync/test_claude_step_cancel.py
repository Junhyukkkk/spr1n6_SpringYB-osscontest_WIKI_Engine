"""쓰기 관문의 취소 안전: 두 번째 취소, git 변경 중 취소, 진입 검사, 실행 환경 예외."""

import asyncio
import os
import stat
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from team_wiki_engine.claude.types import RunClaudeInput, RunClaudeResult
from team_wiki_engine.git.client import GitClient, GitResult
from team_wiki_engine.git.ops import changed_paths
from team_wiki_engine.sync import claude_step
from team_wiki_engine.sync.claude_step import discard_quietly, run_write_step
from team_wiki_engine.sync.lint_loop import lint_loop
from team_wiki_engine.sync.post_check import discard_work_tree
from tests.helpers import write_tree
from tests.helpers.git import init_repo
from tests.helpers.runner import Drain, ScriptedRunner

MANY = 500
TODAY = "2026-10-08"


async def _big_repo(tmp_dir: Path) -> tuple[GitClient, RunClaudeInput]:
    files = {f"wiki/p{i:03d}.md": f"page {i}\n" for i in range(MANY)}
    git = await init_repo(tmp_dir / "data", files)
    inp = RunClaudeInput("sync-code", "p", git.cwd, (), (git.cwd / "wiki",), 1000)
    return git, inp


def _dirty_many(i: RunClaudeInput, n: int) -> None:
    """추적 파일 수정 + 새 파일 — reset·clean이 일을 많이 하게 한다."""
    write_tree(i.cwd, {f"wiki/p{k:03d}.md": f"changed {k}\n" for k in range(MANY)})
    write_tree(i.cwd, {f"wiki/new{k:03d}.md": "n\n" for k in range(MANY)})


async def _assert_pristine(git: GitClient) -> None:
    assert not os.path.lexists(git.cwd / ".git" / "index.lock")
    status = await git.run(["status", "--porcelain", "--ignored", "-uall"])
    assert status.text == ""


def _spy_run(
    monkeypatch: pytest.MonkeyPatch,
    git: GitClient,
    subcommand: str,
    on_enter: Callable[[], Awaitable[None]],
) -> None:
    """`git.run`이 `subcommand`를 처음 시작할 때 `on_enter`를 부르고 진짜 실행으로 이어 간다."""
    real = git.run
    seen: list[int] = []

    async def spy(args: Sequence[str], **kwargs: Any) -> GitResult:
        if subcommand in args and not seen:
            seen.append(1)
            await on_enter()
        return await real(args, **kwargs)

    monkeypatch.setattr(git, "run", spy)


@pytest.fixture
def no_lock_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """오래된 락 복구를 끈다 — 끝까지 돌리는 보호가 빠지면 남은 락 때문에 테스트가 실패한다."""
    monkeypatch.setattr(claude_step, "_remove_stale_index_lock", lambda git: False)


@pytest.mark.usefixtures("no_lock_recovery")
class TestSecondCancel:
    async def test_러너_취소_뒤_정리_drain_중_두_번째_취소에도_되돌린다(
        self, tmp_dir: Path
    ) -> None:
        git, inp = await _big_repo(tmp_dir)
        started = asyncio.Event()
        in_drain = asyncio.Event()
        calls: list[float] = []

        async def runner(i: RunClaudeInput) -> RunClaudeResult:
            write_tree(i.cwd, {"wiki/half.md": "쓰다 만"})
            started.set()
            await asyncio.Event().wait()
            raise AssertionError

        async def drain(t: float) -> int:
            calls.append(t)
            in_drain.set()
            await asyncio.Event().wait()
            return 0

        task = asyncio.create_task(
            run_write_step(
                runner=runner, inp=inp, git=git, data_dir=git.cwd, drain=drain, settle_timeout_s=4.0
            )
        )
        async with asyncio.timeout(20):
            await started.wait()
            task.cancel()
            await in_drain.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert calls == [4.0]
        await _assert_pristine(git)

    async def test_되돌리는_도중_여러_번_취소돼도_끝까지_되돌리고_취소를_전파한다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        git, inp = await _big_repo(tmp_dir)
        started = asyncio.Event()
        in_discard = asyncio.Event()
        gate = asyncio.Event()

        async def runner(i: RunClaudeInput) -> RunClaudeResult:
            _dirty_many(i, 0)
            started.set()
            await asyncio.Event().wait()
            raise AssertionError

        async def hold() -> None:
            in_discard.set()
            await gate.wait()

        _spy_run(monkeypatch, git, "reset", hold)
        drain = Drain()
        task = asyncio.create_task(
            run_write_step(
                runner=runner, inp=inp, git=git, data_dir=git.cwd, drain=drain, settle_timeout_s=1.0
            )
        )
        async with asyncio.timeout(20):
            await started.wait()
            task.cancel()
            await in_discard.wait()
            for _ in range(3):
                task.cancel()
                await asyncio.sleep(0.01)
                assert not task.done()
            gate.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert drain.calls == [1.0]
        await _assert_pristine(git)


@pytest.mark.usefixtures("no_lock_recovery")
class TestCancelDuringGitMutation:
    @pytest.mark.parametrize("delay", [0.0, 0.005, 0.01, 0.02, 0.04])
    async def test_실패_경로의_되돌리기_중_취소해도_락이_남지_않는다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch, delay: float
    ) -> None:
        git, inp = await _big_repo(tmp_dir)
        entered = asyncio.Event()

        async def on_enter() -> None:
            entered.set()

        _spy_run(monkeypatch, git, "reset", on_enter)
        task = asyncio.create_task(
            run_write_step(
                runner=ScriptedRunner(_dirty_many),
                inp=inp,
                git=git,
                data_dir=git.cwd,
                drain=Drain(pending=1),
                settle_timeout_s=1.0,
            )
        )
        async with asyncio.timeout(30):
            await entered.wait()
            await asyncio.sleep(delay)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await _assert_pristine(git)

    @pytest.mark.parametrize("delay", [0.0, 0.005, 0.01, 0.02, 0.04])
    async def test_사후_검사_안의_되돌리기_중_취소해도_락이_남지_않는다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch, delay: float
    ) -> None:
        git, inp = await _big_repo(tmp_dir)
        entered = asyncio.Event()

        async def on_enter() -> None:
            entered.set()

        def script(i: RunClaudeInput, n: int) -> Mapping[str, Any] | None:
            _dirty_many(i, n)
            write_tree(i.cwd, {"raw/x.md": "허용 밖"})
            return None

        _spy_run(monkeypatch, git, "reset", on_enter)
        task = asyncio.create_task(
            run_write_step(
                runner=ScriptedRunner(script),
                inp=inp,
                git=git,
                data_dir=git.cwd,
                drain=Drain(),
                settle_timeout_s=1.0,
            )
        )
        async with asyncio.timeout(30):
            await entered.wait()
            await asyncio.sleep(delay)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await _assert_pristine(git)

    @pytest.mark.parametrize("delay", [0.0, 0.005, 0.01, 0.02, 0.04])
    async def test_점검_루프의_커밋_중_취소해도_락이_남지_않는다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch, delay: float
    ) -> None:
        git = await init_repo(tmp_dir / "data", {"wiki/index.md": "# frontmatter 없음\n"})
        inp = RunClaudeInput("sync-code", "p", git.cwd, (), (git.cwd / "wiki",), 1000)
        entered = asyncio.Event()

        async def on_enter() -> None:
            entered.set()

        def script(i: RunClaudeInput, n: int) -> Mapping[str, Any] | None:
            write_tree(i.cwd, {f"wiki/n{k:03d}.md": "n\n" for k in range(MANY)})
            return None

        _spy_run(monkeypatch, git, "add", on_enter)
        task = asyncio.create_task(
            lint_loop(
                data_dir=git.cwd,
                git=git,
                runner=ScriptedRunner(script),
                inp=inp,
                session_id="s",
                reapply=lambda: None,
                fix_commit_message="m",
                today=TODAY,
                drain=Drain(),
                settle_timeout_s=1.0,
            )
        )
        async with asyncio.timeout(30):
            await entered.wait()
            await asyncio.sleep(delay)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await _assert_pristine(git)


def _stale_mtimes(git: GitClient) -> None:
    """인덱스의 mtime 기록이 낡게 만든다 — `status`가 인덱스를 새로 쓰려 한다."""
    for k in range(MANY):
        path = git.cwd / "wiki" / f"p{k:03d}.md"
        os.utime(path, (1_000_000 + k, 1_000_000 + k))


class TestEntryStatusCancel:
    async def test_진입_검사_status_중_취소해도_락이_남지_않는다(self, tmp_dir: Path) -> None:
        git, inp = await _big_repo(tmp_dir)
        async with asyncio.timeout(120):
            for i in range(41):  # 0~120ms
                _stale_mtimes(git)
                task = asyncio.create_task(
                    run_write_step(
                        runner=ScriptedRunner(),
                        inp=inp,
                        git=git,
                        data_dir=git.cwd,
                        drain=Drain(),
                        settle_timeout_s=1.0,
                    )
                )
                await asyncio.sleep(i * 0.003)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                assert not os.path.lexists(git.cwd / ".git" / "index.lock"), i

    async def test_changed_paths_자체도_취소에_락을_남기지_않는다(self, tmp_dir: Path) -> None:
        # 다른 호출자(작업 사본 점검 등)가 보호 없이 부르는 경우: --no-optional-locks가 막는다
        git, _ = await _big_repo(tmp_dir)
        async with asyncio.timeout(120):
            for i in range(60):  # 0~118ms
                _stale_mtimes(git)
                task = asyncio.create_task(changed_paths(git))
                await asyncio.sleep(i * 0.002)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                assert not os.path.lexists(git.cwd / ".git" / "index.lock"), i


class TestDiscardQuietly:
    async def test_오래된_index_lock을_지우고_한_번_다시_시도한다(
        self, tmp_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        git, _ = await _big_repo(tmp_dir)
        write_tree(git.cwd, {"wiki/p000.md": "dirty", "wiki/new.md": "n"})
        (git.cwd / ".git" / "index.lock").write_text("")
        await discard_quietly(git)
        await _assert_pristine(git)
        warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert any("index.lock" in m for m in warnings)
        assert all(str(tmp_dir) not in m for m in warnings)  # 경로를 싣지 않는다

    async def test_index_lock이_링크면_따라가지_않고_지우지_않는다(self, tmp_dir: Path) -> None:
        git, _ = await _big_repo(tmp_dir)
        target = tmp_dir / "victim"
        target.write_text("지키기")
        lock = git.cwd / ".git" / "index.lock"
        lock.symlink_to(target)
        write_tree(git.cwd, {"wiki/p000.md": "dirty"})
        await discard_quietly(git)  # 실패는 삼킨다
        assert target.read_text() == "지키기"
        assert lock.is_symlink()

    async def test_링크로_넘어온_작업_사본이면_다른_저장소의_락을_지우지_않는다(
        self, tmp_dir: Path
    ) -> None:
        other, _ = await _big_repo(tmp_dir)
        lock = other.cwd / ".git" / "index.lock"
        lock.write_text("")
        link = tmp_dir / "link"
        link.symlink_to(other.cwd)
        await discard_quietly(GitClient(link, allowed_protocols="file"))
        assert lock.exists()  # 가드가 링크 작업 사본을 거부하고, 락도 건드리지 않는다

    async def test_HEAD가_이상하면_락을_지우지_않는다(self, tmp_dir: Path) -> None:
        git, _ = await _big_repo(tmp_dir)
        lock = git.cwd / ".git" / "index.lock"
        lock.write_text("")
        (git.cwd / ".git" / "HEAD").write_text("garbage")
        await discard_quietly(git)
        assert lock.exists()

    async def test_취소로_끝난_되돌리기는_성공으로_세지_않고_다시_시도한다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        git, _ = await _big_repo(tmp_dir)
        write_tree(git.cwd, {"wiki/p000.md": "dirty", "wiki/new.md": "n"})
        real = discard_work_tree
        calls: list[int] = []

        async def flaky(g: GitClient) -> None:
            calls.append(1)
            if len(calls) == 1:
                raise asyncio.CancelledError
            await real(g)

        monkeypatch.setattr(claude_step, "discard_work_tree", flaky)
        await discard_quietly(git)
        assert len(calls) == 2
        await _assert_pristine(git)

    async def test_계속_취소로_끝나면_취소를_전파한다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        git, _ = await _big_repo(tmp_dir)

        async def always(g: GitClient) -> None:
            raise asyncio.CancelledError

        monkeypatch.setattr(claude_step, "discard_work_tree", always)
        with pytest.raises(asyncio.CancelledError):
            await discard_quietly(git)

    async def test_되돌리기_실패는_삼키고_취소는_전파한다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        git, _ = await _big_repo(tmp_dir)

        async def boom(args: Sequence[str], **kw: Any) -> GitResult:
            raise RuntimeError("git broke")

        monkeypatch.setattr(git, "run", boom)
        await discard_quietly(git)  # 예외 없음


class TestEntryChecks:
    async def test_커밋되지_않은_변경이_있으면_러너를_부르지_않는다(self, tmp_dir: Path) -> None:
        git, inp = await _big_repo(tmp_dir)
        write_tree(git.cwd, {"wiki/p000.md": "남은 변경"})
        runner = ScriptedRunner()
        drain = Drain()
        step = await run_write_step(
            runner=runner, inp=inp, git=git, data_dir=git.cwd, drain=drain, settle_timeout_s=1.0
        )
        assert step.status == "unsafe_write_root" and step.run is None
        assert step.message == "작업 사본에 커밋되지 않은 변경이 있습니다"
        assert runner.inputs == [] and drain.calls == []
        assert (git.cwd / "wiki" / "p000.md").read_text() == "남은 변경"  # 건드리지 않는다

    async def test_무시된_파일도_변경으로_본다(self, tmp_dir: Path) -> None:
        git = await init_repo(tmp_dir / "data", {"wiki/a.md": "a", ".gitignore": "*.tmp\n"})
        write_tree(git.cwd, {"wiki/x.tmp": "x"})
        runner = ScriptedRunner()
        step = await run_write_step(
            runner=runner,
            inp=RunClaudeInput("sync-code", "p", git.cwd, (), (git.cwd / "wiki",), 1000),
            git=git,
            data_dir=git.cwd,
            drain=Drain(),
            settle_timeout_s=1.0,
        )
        assert step.status == "unsafe_write_root"
        assert runner.inputs == []

    @pytest.mark.parametrize("which", ["inp", "data_dir"])
    async def test_경로가_서로_다르면_러너를_부르지_않는다(self, tmp_dir: Path, which: str) -> None:
        git, inp = await _big_repo(tmp_dir)
        other = tmp_dir / "other"
        other.mkdir()
        (other / "wiki").mkdir()
        runner = ScriptedRunner()
        drain = Drain()
        step = await run_write_step(
            runner=runner,
            inp=inp
            if which == "data_dir"
            else RunClaudeInput("sync-code", "p", other, (), (other / "wiki",), 1000),
            git=git,
            data_dir=other if which == "data_dir" else git.cwd,
            drain=drain,
            settle_timeout_s=1.0,
        )
        assert step.status == "unsafe_write_root" and step.run is None
        assert step.message == "경로가 서로 다릅니다"
        assert runner.inputs == [] and drain.calls == []

    async def test_경로가_다르면_다른_폴더에_wiki를_만들지_않는다(self, tmp_dir: Path) -> None:
        git, inp = await _big_repo(tmp_dir)
        other = tmp_dir / "other"
        other.mkdir()
        step = await run_write_step(
            runner=ScriptedRunner(),
            inp=inp,
            git=git,
            data_dir=other,
            drain=Drain(),
            settle_timeout_s=1.0,
        )
        assert step.message == "경로가 서로 다릅니다"
        assert not (other / "wiki").exists()

    async def test_깨끗한_작업_사본이면_통과한다(self, tmp_dir: Path) -> None:
        git, inp = await _big_repo(tmp_dir)
        assert await changed_paths(git) == []
        step = await run_write_step(
            runner=ScriptedRunner(),
            inp=inp,
            git=git,
            data_dir=git.cwd,
            drain=Drain(),
            settle_timeout_s=1.0,
        )
        assert step.status == "ok"


class _Suspend:
    def __await__(self) -> Any:
        yield


def _drive_by_hand(monkeypatch: pytest.MonkeyPatch) -> None:
    """코루틴을 `send`로 직접 몰 수 있게 진입 검사가 이벤트 루프를 기다리지 않게 한다."""

    async def clean_tree(g: GitClient) -> list[str]:
        return []

    async def passthrough(coro: Any) -> Any:
        return await coro

    monkeypatch.setattr(claude_step, "changed_paths", clean_tree)
    monkeypatch.setattr(claude_step, "run_to_completion", passthrough, raising=False)


class TestBaseExceptions:
    async def test_코루틴을_닫아도_RuntimeError가_나지_않는다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        git, inp = await _big_repo(tmp_dir)

        _drive_by_hand(monkeypatch)

        async def runner(i: RunClaudeInput) -> RunClaudeResult:
            await _Suspend()
            raise AssertionError

        coro = run_write_step(
            runner=runner,
            inp=inp,
            git=git,
            data_dir=git.cwd,
            drain=Drain(),
            settle_timeout_s=1.0,
        )
        coro.send(None)  # 러너 안에서 멈춘다
        coro.close()  # GeneratorExit: 기다리는 정리를 시도하면 RuntimeError

    async def test_정리_drain_중_코루틴을_닫아도_RuntimeError_없이_되돌린다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        git, inp = await _big_repo(tmp_dir)
        _drive_by_hand(monkeypatch)
        calls: list[float] = []

        async def runner(i: RunClaudeInput) -> RunClaudeResult:
            write_tree(i.cwd, {"wiki/half.md": "x"})
            await _Suspend()
            raise AssertionError

        async def drain(t: float) -> int:
            calls.append(t)
            await _Suspend()
            return 0

        coro = run_write_step(
            runner=runner, inp=inp, git=git, data_dir=git.cwd, drain=drain, settle_timeout_s=1.0
        )
        coro.send(None)  # 러너 안에서 멈춘다
        coro.throw(KeyboardInterrupt)  # 정리 drain 안에서 멈춘다
        assert calls == [1.0]
        coro.close()  # GeneratorExit가 drain의 await에 던져진다: 기다리지 말고 예약해야 한다
        async with asyncio.timeout(30):
            while await changed_paths(git):  # 예약한 되돌리기가 돈다
                await asyncio.sleep(0.05)
        await _assert_pristine(git)

    async def test_KeyboardInterrupt도_drain하고_되돌린_뒤_전파한다(self, tmp_dir: Path) -> None:
        git, inp = await _big_repo(tmp_dir)

        async def runner(i: RunClaudeInput) -> RunClaudeResult:
            write_tree(i.cwd, {"wiki/half.md": "x"})
            raise KeyboardInterrupt

        drain = Drain()
        with pytest.raises(KeyboardInterrupt):
            await run_write_step(
                runner=runner, inp=inp, git=git, data_dir=git.cwd, drain=drain, settle_timeout_s=2.0
            )
        assert drain.calls == [2.0]
        await _assert_pristine(git)


@pytest.mark.skipif(os.geteuid() == 0, reason="root는 권한 없는 폴더도 읽는다")
class TestUnreadableDirsOnFailurePaths:
    @pytest.mark.parametrize("path", ["claude_failed", "unsettled"])
    async def test_읽을_수_없는_폴더가_있어도_되돌리고_결과로_끝난다(
        self, tmp_dir: Path, path: str
    ) -> None:
        git, inp = await _big_repo(tmp_dir)

        def script(i: RunClaudeInput, n: int) -> Mapping[str, Any] | None:
            write_tree(i.cwd, {"wiki/locked/inner/f.md": "x"})
            os.chmod(i.cwd / "wiki" / "locked" / "inner", 0)
            os.chmod(i.cwd / "wiki" / "locked", 0)
            return {"ok": False, "failure": "timeout"} if path == "claude_failed" else None

        step = await run_write_step(
            runner=ScriptedRunner(script),
            inp=inp,
            git=git,
            data_dir=git.cwd,
            drain=Drain(pending=1 if path == "unsettled" else 0),
            settle_timeout_s=1.0,
        )
        assert step.status == path
        await _assert_pristine(git)
        assert not (git.cwd / "wiki" / "locked").exists()
        _ = stat
