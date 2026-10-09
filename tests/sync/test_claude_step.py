"""Claude 쓰기 관문(`run_write_step`). 동기화의 모든 Claude 쓰기가 지나는 보안 경계다."""

import asyncio
import dataclasses
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from team_wiki_engine.claude.types import RunClaudeInput, RunClaudeResult
from team_wiki_engine.git.client import GitClient
from team_wiki_engine.git.ops import changed_paths
from team_wiki_engine.sync.claude_step import (
    WriteStep,
    claude_failure_message,
    run_write_step,
)
from tests.helpers import write_tree
from tests.helpers.git import init_repo
from tests.helpers.runner import ScriptedRunner, ok_result

SECRET_A = "sk-ant-api03-" + "AbCdEfGh1234" * 4
SECRET_G = "ghp_" + "A1b2C3d4E5f6G7h8I9j0" * 2


class Drain:
    """`drain(timeout)`을 부른 횟수와 인자를 기록하고 정해 둔 개수를 돌려준다."""

    def __init__(self, pending: int = 0) -> None:
        self.pending = pending
        self.calls: list[float] = []

    async def __call__(self, timeout: float) -> int:
        self.calls.append(timeout)
        return self.pending


async def _repo(tmp_dir: Path) -> tuple[GitClient, RunClaudeInput]:
    git = await init_repo(tmp_dir / "data", {"wiki/a.md": "a", "raw/r.md": "r"})
    inp = RunClaudeInput(
        profile="sync-code",
        prompt="p",
        cwd=git.cwd,
        read_roots=(),
        write_roots=(git.cwd / "wiki",),
        timeout_ms=1000,
    )
    return git, inp


async def _step(
    git: GitClient,
    inp: RunClaudeInput,
    runner: ScriptedRunner,
    drain: Drain,
    settle: float = 7.5,
) -> WriteStep:
    return await run_write_step(
        runner=runner, inp=inp, git=git, data_dir=git.cwd, drain=drain, settle_timeout_s=settle
    )


class TestRunWriteStepOk:
    async def test_허용_범위_변경은_커밋하지_않은_채_ok다(self, tmp_dir: Path) -> None:
        git, inp = await _repo(tmp_dir)
        runner = ScriptedRunner(lambda i, n: write_tree(i.cwd, {"wiki/new.md": "n"}))
        drain = Drain()
        step = await _step(git, inp, runner, drain)
        assert step.status == "ok"
        assert step.run is not None and step.run.ok
        assert step.message == ""
        assert await changed_paths(git) == ["wiki/new.md"]
        head = (await git.run(["log", "--format=%s"])).text.split("\n")
        assert head[0] == "init"  # 커밋하지 않았다

    async def test_empty이고_변경이_없으면_ok다(self, tmp_dir: Path) -> None:
        git, inp = await _repo(tmp_dir)
        runner = ScriptedRunner(lambda i, n: {"ok": False, "failure": "empty"})
        step = await _step(git, inp, runner, Drain())
        assert step.status == "ok"
        assert step.run is not None and step.run.failure == "empty"
        assert await changed_paths(git) == []

    async def test_empty여도_허용_밖_변경은_post_check_failed다(self, tmp_dir: Path) -> None:
        git, inp = await _repo(tmp_dir)

        def script(i: RunClaudeInput, n: int) -> Mapping[str, Any]:
            write_tree(i.cwd, {"raw/x.md": "x"})
            return {"ok": False, "failure": "empty"}

        step = await _step(git, inp, ScriptedRunner(script), Drain())
        assert step.status == "post_check_failed"
        assert await changed_paths(git) == []


class TestRunWriteStepOrder:
    async def test_쓰기_루트가_링크면_러너를_부르지_않는다(self, tmp_dir: Path) -> None:
        git, inp = await _repo(tmp_dir)
        outside = tmp_dir / "outside"
        outside.mkdir()
        os.rename(git.cwd / "wiki", tmp_dir / "moved")
        os.symlink(outside, git.cwd / "wiki")
        runner = ScriptedRunner()
        drain = Drain()
        step = await _step(git, inp, runner, drain)
        assert step.status == "unsafe_write_root"
        assert step.run is None
        assert step.message
        assert runner.inputs == []
        assert drain.calls == []  # 부르지 않았으니 기다릴 것도 없다

    async def test_wiki_안에_링크가_있으면_러너를_부르지_않는다(self, tmp_dir: Path) -> None:
        git, inp = await _repo(tmp_dir)
        os.symlink(tmp_dir, git.cwd / "wiki" / "escape")
        runner = ScriptedRunner()
        step = await _step(git, inp, runner, Drain())
        assert step.status == "unsafe_write_root"
        assert runner.inputs == []

    async def test_drain이_남았다고_하면_unsettled이고_파일을_버린다(self, tmp_dir: Path) -> None:
        git, inp = await _repo(tmp_dir)
        runner = ScriptedRunner(lambda i, n: write_tree(i.cwd, {"wiki/half.md": "쓰다 만"}))
        step = await _step(git, inp, runner, Drain(pending=1))
        assert step.status == "unsettled"
        assert step.message == "정리되지 않은 Claude 실행이 남아 커밋하지 않음"
        assert step.run is not None
        assert not (git.cwd / "wiki" / "half.md").exists()
        assert await changed_paths(git) == []

    async def test_unsettled가_claude_failed보다_먼저다(self, tmp_dir: Path) -> None:
        git, inp = await _repo(tmp_dir)
        runner = ScriptedRunner(lambda i, n: {"ok": False, "failure": "timeout"})
        step = await _step(git, inp, runner, Drain(pending=2))
        assert step.status == "unsettled"

    async def test_drain은_실행마다_정확히_한_번_settle_timeout으로_불린다(
        self, tmp_dir: Path
    ) -> None:
        git, inp = await _repo(tmp_dir)
        for script in (
            lambda i, n: None,
            lambda i, n: {"ok": False, "failure": "error"},
            lambda i, n: write_tree(i.cwd, {"raw/x.md": "x"}),
        ):
            drain = Drain()
            await _step(git, inp, ScriptedRunner(script), drain, settle=3.25)
            assert drain.calls == [3.25]

    async def test_drain은_러너가_끝난_뒤에_불린다(self, tmp_dir: Path) -> None:
        git, inp = await _repo(tmp_dir)
        events: list[str] = []

        async def runner(i: RunClaudeInput) -> RunClaudeResult:
            events.append("run")
            return ok_result()

        async def drain(t: float) -> int:
            events.append("drain")
            return 0

        await run_write_step(
            runner=runner, inp=inp, git=git, data_dir=git.cwd, drain=drain, settle_timeout_s=1.0
        )
        assert events == ["run", "drain"]

    async def test_러너에_넘기는_입력은_그대로다(self, tmp_dir: Path) -> None:
        git, inp = await _repo(tmp_dir)
        runner = ScriptedRunner()
        await _step(git, inp, runner, Drain())
        assert runner.inputs == [inp]


class TestRunWriteStepFailure:
    async def test_실패하면_되돌리고_claude_failed다(self, tmp_dir: Path) -> None:
        git, inp = await _repo(tmp_dir)

        def script(i: RunClaudeInput, n: int) -> Mapping[str, Any]:
            write_tree(i.cwd, {"wiki/half.md": "쓰다 만", "wiki/a.md": "changed"})
            return {"ok": False, "failure": "timeout", "detail": "시간 초과"}

        step = await _step(git, inp, ScriptedRunner(script), Drain())
        assert step.status == "claude_failed"
        assert step.message == "Claude timeout: 시간 초과"
        assert step.run is not None and step.run.failure == "timeout"
        assert not (git.cwd / "wiki" / "half.md").exists()
        assert (git.cwd / "wiki" / "a.md").read_text() == "a"

    async def test_실패_메시지에_비밀값이_없고_300자_이하다(self, tmp_dir: Path) -> None:
        git, inp = await _repo(tmp_dir)
        detail = f"auth failed key={SECRET_A} token {SECRET_G} " + "x" * 2000
        runner = ScriptedRunner(lambda i, n: {"ok": False, "failure": "error", "detail": detail})
        step = await _step(git, inp, runner, Drain())
        assert step.status == "claude_failed"
        assert SECRET_A not in step.message
        assert SECRET_G not in step.message
        assert "AbCdEfGh1234" not in step.message
        assert len(step.message) <= 300 + len("Claude error: ") + 1

    async def test_post_check_failed는_위반과_메시지를_담고_되돌린다(self, tmp_dir: Path) -> None:
        git, inp = await _repo(tmp_dir)
        runner = ScriptedRunner(
            lambda i, n: write_tree(i.cwd, {"wiki/a.md": "ok", "wiki/log.md": "forged"})
        )
        step = await _step(git, inp, runner, Drain())
        assert step.status == "post_check_failed"
        assert step.offending == ("wiki/log.md",)
        assert step.message == "허용 경로 밖 변경: wiki/log.md"
        assert await changed_paths(git) == []
        assert (git.cwd / "wiki" / "a.md").read_text() == "a"

    async def test_wiki_안_링크도_post_check_failed다(self, tmp_dir: Path) -> None:
        git, inp = await _repo(tmp_dir)

        def script(i: RunClaudeInput, n: int) -> None:
            os.symlink(tmp_dir, i.cwd / "wiki" / "escape")

        step = await _step(git, inp, ScriptedRunner(script), Drain())
        assert step.status == "post_check_failed"
        assert not os.path.lexists(git.cwd / "wiki" / "escape")

    async def test_위반_경로의_비밀값과_제어_문자는_메시지에_없다(self, tmp_dir: Path) -> None:
        git, inp = await _repo(tmp_dir)
        runner = ScriptedRunner(lambda i, n: write_tree(i.cwd, {f"raw/{SECRET_G}‮.md": "x"}))
        step = await _step(git, inp, runner, Drain())
        assert step.status == "post_check_failed"
        assert SECRET_G not in step.message
        assert "‮" not in step.message
        assert step.message.isprintable()


class TestRunWriteStepExceptions:
    async def test_러너가_예외를_던지면_drain하고_되돌리고_claude_failed다(
        self, tmp_dir: Path
    ) -> None:
        git, inp = await _repo(tmp_dir)

        async def runner(i: RunClaudeInput) -> RunClaudeResult:
            write_tree(i.cwd, {"wiki/half.md": "쓰다 만"})
            raise RuntimeError(f"boom {SECRET_A}")

        drain = Drain()
        step = await run_write_step(
            runner=runner, inp=inp, git=git, data_dir=git.cwd, drain=drain, settle_timeout_s=2.0
        )
        assert step.status == "claude_failed"
        assert step.run is not None and not step.run.ok and step.run.failure == "error"
        assert SECRET_A not in step.message and SECRET_A not in repr(step.run)
        assert "RuntimeError" in step.message
        assert drain.calls == [2.0]
        assert await changed_paths(git) == []

    async def test_러너_예외_뒤에도_drain이_남았다고_하면_unsettled다(self, tmp_dir: Path) -> None:
        git, inp = await _repo(tmp_dir)

        async def runner(i: RunClaudeInput) -> RunClaudeResult:
            raise RuntimeError("x")

        step = await run_write_step(
            runner=runner,
            inp=inp,
            git=git,
            data_dir=git.cwd,
            drain=Drain(pending=1),
            settle_timeout_s=1.0,
        )
        assert step.status == "unsettled"

    async def test_drain이_예외를_던지면_되돌리고_unsettled다(self, tmp_dir: Path) -> None:
        git, inp = await _repo(tmp_dir)

        async def drain(t: float) -> int:
            raise RuntimeError("drain broke")

        runner = ScriptedRunner(lambda i, n: write_tree(i.cwd, {"wiki/half.md": "x"}))
        step = await run_write_step(
            runner=runner, inp=inp, git=git, data_dir=git.cwd, drain=drain, settle_timeout_s=1.0
        )
        assert step.status == "unsettled"
        assert await changed_paths(git) == []

    async def test_사후_검사가_예외를_던지면_되돌리고_예외가_전파된다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from team_wiki_engine.sync import claude_step

        git, inp = await _repo(tmp_dir)

        async def broken(g: GitClient, *_a: object) -> object:
            raise RuntimeError("status failed")

        monkeypatch.setattr(claude_step, "verify_claude_changes", broken)
        runner = ScriptedRunner(lambda i, n: write_tree(i.cwd, {"wiki/half.md": "x"}))
        with pytest.raises(RuntimeError, match="status failed"):
            await _step(git, inp, runner, Drain())
        assert await changed_paths(git) == []


class TestRunWriteStepCancellation:
    async def test_러너_실행_중_취소하면_drain하고_되돌린_뒤_취소가_전파된다(
        self, tmp_dir: Path
    ) -> None:
        git, inp = await _repo(tmp_dir)
        started = asyncio.Event()

        async def runner(i: RunClaudeInput) -> RunClaudeResult:
            write_tree(i.cwd, {"wiki/half.md": "쓰다 만"})
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("도달하지 않는다")

        drain = Drain()
        task = asyncio.create_task(
            run_write_step(
                runner=runner,
                inp=inp,
                git=git,
                data_dir=git.cwd,
                drain=drain,
                settle_timeout_s=4.0,
            )
        )
        async with asyncio.timeout(10):
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert drain.calls == [4.0]
        assert not (git.cwd / "wiki" / "half.md").exists()
        assert await changed_paths(git) == []

    async def test_drain_중_취소하면_다시_drain하지_않고_되돌린다(self, tmp_dir: Path) -> None:
        git, inp = await _repo(tmp_dir)
        in_drain = asyncio.Event()
        calls: list[float] = []

        async def drain(t: float) -> int:
            calls.append(t)
            in_drain.set()
            await asyncio.Event().wait()
            return 0

        runner = ScriptedRunner(lambda i, n: write_tree(i.cwd, {"wiki/half.md": "x"}))
        task = asyncio.create_task(
            run_write_step(
                runner=runner,
                inp=inp,
                git=git,
                data_dir=git.cwd,
                drain=drain,
                settle_timeout_s=4.0,
            )
        )
        async with asyncio.timeout(10):
            await in_drain.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert calls == [4.0]
        assert await changed_paths(git) == []

    async def test_사후_검사_중_취소해도_되돌린다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from team_wiki_engine.sync import claude_step

        git, inp = await _repo(tmp_dir)
        in_check = asyncio.Event()

        async def slow(g: GitClient, *_a: object) -> object:
            in_check.set()
            await asyncio.Event().wait()
            return None

        monkeypatch.setattr(claude_step, "verify_claude_changes", slow)
        runner = ScriptedRunner(lambda i, n: write_tree(i.cwd, {"wiki/half.md": "x"}))
        drain = Drain()
        task = asyncio.create_task(_step(git, inp, runner, drain))
        async with asyncio.timeout(10):
            await in_check.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert drain.calls == [7.5]
        assert await changed_paths(git) == []


class TestClaudeFailureMessage:
    def test_종류와_자세한_내용(self) -> None:
        r = ok_result(ok=False, failure="max_turns", detail="너무 길다")
        assert claude_failure_message(r) == "Claude max_turns: 너무 길다"

    def test_detail이_없으면_종류만(self) -> None:
        assert claude_failure_message(ok_result(ok=False, failure="timeout")) == "Claude timeout"

    def test_failure가_없으면_error다(self) -> None:
        assert claude_failure_message(ok_result(ok=False)) == "Claude error"

    def test_detail은_가리고_자른다(self) -> None:
        r = ok_result(ok=False, failure="error", detail=f"{SECRET_A}\n" + "y" * 1000)
        message = claude_failure_message(r)
        assert SECRET_A not in message
        assert "\n" not in message
        assert len(message) <= len("Claude error: ") + 301

    def test_알려진_비밀값은_자르기_전에_가려_조각이_남지_않는다(self) -> None:
        opaque = "correcthorsebatterystaple"  # 토큰 모양이 아닌 값
        for pad in range(270, 300):  # 300자 경계 앞뒤 어디에 걸쳐도
            r = ok_result(ok=False, failure="error", detail="가" * pad + opaque + " 끝")
            message = claude_failure_message(r, (opaque,))
            assert "correcth" not in message, pad
            assert opaque not in message, pad

    def test_빈_detail은_붙이지_않는다(self) -> None:
        assert claude_failure_message(ok_result(ok=False, failure="error", detail="")) == (
            "Claude error"
        )


class TestCapOrder:
    def test_cap은_가린_뒤_자른다(self) -> None:
        from team_wiki_engine.sync import claude_step

        opaque = "correcthorsebatterystaple"
        for pad in range(975, 1001):  # 1000자 경계에 걸친다
            capped = claude_step._cap("x" * pad + opaque + " 끝", (opaque,))
            assert "correcth" not in capped, pad


class TestWriteStepRedact:
    async def test_redact를_넘기면_실패_메시지의_알려진_비밀값을_자르기_전에_가린다(
        self, tmp_dir: Path
    ) -> None:
        git, inp = await _repo(tmp_dir)
        opaque = "correcthorsebatterystaple"
        detail = "가" * 288 + opaque + " 끝"
        runner = ScriptedRunner(lambda i, n: {"ok": False, "failure": "error", "detail": detail})
        step = await run_write_step(
            runner=runner,
            inp=inp,
            git=git,
            data_dir=git.cwd,
            drain=Drain(),
            settle_timeout_s=1.0,
            redact=(opaque,),
        )
        assert step.status == "claude_failed"
        assert "correcth" not in step.message

    async def test_redact를_넘기면_위반_경로의_비밀값도_가린다(self, tmp_dir: Path) -> None:
        git, inp = await _repo(tmp_dir)
        opaque = "correcthorsebatterystaple"
        runner = ScriptedRunner(lambda i, n: write_tree(i.cwd, {f"raw/{opaque}.md": "x"}))
        step = await run_write_step(
            runner=runner,
            inp=inp,
            git=git,
            data_dir=git.cwd,
            drain=Drain(),
            settle_timeout_s=1.0,
            redact=(opaque,),
        )
        assert step.status == "post_check_failed"
        assert opaque not in step.message


class TestWriteStepShape:
    def test_frozen이고_기본값이_있다(self) -> None:
        step = WriteStep("ok", None)
        assert step.offending == () and step.message == ""
        with pytest.raises(dataclasses.FrozenInstanceError):
            step.status = "claude_failed"  # type: ignore[misc]


class TestScriptedRunner:
    async def test_입력을_기록하고_스크립트_결과를_반영한다(self, tmp_dir: Path) -> None:
        seen: list[int] = []

        def script(inp: RunClaudeInput, n: int) -> Mapping[str, Any]:
            seen.append(n)
            return {"ok": False, "failure": "timeout"}

        runner = ScriptedRunner(script)
        inp = RunClaudeInput("bot", "p", tmp_dir, (), (), 1)
        result = await runner(inp)
        assert runner.inputs == [inp]
        assert result.ok is False and result.failure == "timeout"
        assert result.text == "완료" and result.session_id == "sess-1"
        await runner(inp)
        assert seen == [0, 1]

    async def test_스크립트가_없으면_ok_result이고_나중에_바꿀_수_있다(self, tmp_dir: Path) -> None:
        runner = ScriptedRunner()
        inp = RunClaudeInput("bot", "p", tmp_dir, (), (), 1)
        assert (await runner(inp)) == ok_result()
        runner.set_script(lambda i, n: {"text": "바뀜"})
        assert (await runner(inp)).text == "바뀜"

    def test_ok_result_기본값(self) -> None:
        r = ok_result()
        assert (r.ok, r.text, r.session_id, r.cost_usd, r.duration_ms, r.tool_calls) == (
            True,
            "완료",
            "sess-1",
            0.01,
            5,
            [],
        )
        assert (r.usage.input_tokens, r.usage.output_tokens) == (100, 10)
        assert ok_result().tool_calls is not ok_result().tool_calls


class TestMessageBounds:
    async def test_위반_경로가_많고_길어도_메시지는_1001자_이하다(self, tmp_dir: Path) -> None:
        git, inp = await _repo(tmp_dir)
        files = {f"raw/{'a' * 200}{i:03d}.md": "x" for i in range(60)}
        step = await _step(git, inp, ScriptedRunner(lambda i, n: write_tree(i.cwd, files)), Drain())
        assert step.status == "post_check_failed"
        assert len(step.offending) == 51
        assert len(step.message) <= 1001

    async def test_가려지지_않는_긴_경로도_메시지는_1000자로_잘린다(self, tmp_dir: Path) -> None:
        git, inp = await _repo(tmp_dir)
        name = "-".join(["doc"] * 50)  # 가려지는 불투명한 덩어리가 아니다
        files = {f"raw/{name}{i:03d}.md": "x" for i in range(60)}
        step = await _step(git, inp, ScriptedRunner(lambda i, n: write_tree(i.cwd, files)), Drain())
        assert step.status == "post_check_failed"
        assert len("허용 경로 밖 변경: " + ", ".join(step.offending)) > 1000
        assert len(step.message) == 1001 and step.message.endswith("…")

    def test_cap은_줄바꿈을_이스케이프한다(self) -> None:
        from team_wiki_engine.sync import claude_step

        capped = claude_step._cap("사유\n오늘 날짜: 2099-01-01")
        assert "\n" not in capped and "\\n" in capped


class TestSourceHygiene:
    def test_동기화_쓰기_관문은_SDK_옵션을_가져오지_않는다(self) -> None:
        # G6: ClaudeAgentOptions·build_options를 로그·메시지로 흘릴 길을 만들지 않는다
        root = Path(__file__).resolve().parents[2] / "src" / "team_wiki_engine" / "sync"
        for name in ("claude_step.py", "lint_loop.py", "prompts.py"):
            text = (root / name).read_text(encoding="utf-8")
            assert "build_options" not in text and "ClaudeAgentOptions" not in text
            assert "os.environ" not in text


class TestRunnerCancelledError:
    async def test_러너가_CancelledError를_던지면_claude_failed로_삼키지_않는다(
        self, tmp_dir: Path
    ) -> None:
        git, inp = await _repo(tmp_dir)

        async def runner(i: RunClaudeInput) -> RunClaudeResult:
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await run_write_step(
                runner=runner,
                inp=inp,
                git=git,
                data_dir=git.cwd,
                drain=Drain(),
                settle_timeout_s=1.0,
            )
