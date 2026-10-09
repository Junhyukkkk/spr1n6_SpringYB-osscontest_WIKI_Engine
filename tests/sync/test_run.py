"""한 번의 동기화 실행(`run_once`)·보고 형식·단계 안전 실행 시험(Task 16).

저장소는 임시 폴더의 실제 bare 저장소, Claude·알림은 가짜다. 코드·문서 단계 자체는 Task 14·15가
시험하므로 여기서는 조립(잠금·측정·정리 대기·기록·보고)을 본다.
"""

import asyncio
import dataclasses
import os
from datetime import timedelta
from pathlib import Path

import pytest

from team_wiki_engine.git.client import GitError
from team_wiki_engine.metrics.record import STATE_FILES, SyncRunRecord, read_jsonl
from team_wiki_engine.sync import ingest as ingest_module
from team_wiki_engine.sync.lock import acquire_lock, read_lock_owner
from team_wiki_engine.sync.run import (
    SyncRunError,
    SyncRunReport,
    format_report,
    run_once,
    safe_stage,
)
from team_wiki_engine.sync.stage import StageContext
from team_wiki_engine.sync.types import StageResult, SyncKind, new_stage
from tests.helpers.runner import Drain
from tests.helpers.sync_env import DEFAULT_NOW, SyncEnv, make_sync_env

PATCH = "team_wiki_engine.sync.run."
RUN_ID = "20261007-0900"
SECRET = "ghp_Zx81QmN4vT7bK2pLs9Wd3Yc6Hf0JrAeU5"
RAW = {"raw/2026-10-07-회의록.md": "# 회의록"}
BUSY = "[sync] 이미 실행 중이라 건너뜀"
UNSETTLED = "이전 Claude 실행이 정리되지 않아 문서 반영을 건너뜀"


def stage_of(kind: SyncKind, result: str = "pr", summary: str = "") -> StageResult:
    stage = new_stage(kind)
    stage.result = result  # type: ignore[assignment]
    stage.summary = summary or f"{kind}: 가짜"
    return stage


def records(env: SyncEnv) -> list[SyncRunRecord]:
    return read_jsonl(env.paths.state_dir / STATE_FILES.sync_runs, SyncRunRecord)


def lock_is_free(env: SyncEnv) -> bool:
    lock = acquire_lock(env.paths.state_dir / STATE_FILES.lock, now=DEFAULT_NOW)
    if lock is None:
        return False
    lock.release()
    return True


# ---------------------------------------------------------------- format_report


class TestFormatReport:
    def test_단계_요약과_비용_합계를_한_줄로_만든다(self) -> None:
        a = stage_of("code", summary="코드: 변경 3건 → #12")
        a.cost_usd = 0.3
        b = stage_of("ingest", "no-change", "문서: 없음")
        b.cost_usd = 0.11
        assert (
            format_report("20261007-0900", [a, b])
            == "[sync 20261007-0900] 코드: 변경 3건 → #12 · 문서: 없음 · 비용 $0.41"
        )

    def test_단계가_없으면_비용만(self) -> None:
        assert format_report("20261007-0900", []) == "[sync 20261007-0900] 비용 $0.00"


# ---------------------------------------------------------------- safe_stage


class TestSafeStage:
    async def test_정상_결과는_그대로(self) -> None:
        expected = stage_of("code")

        async def ok() -> StageResult:
            return expected

        assert await safe_stage("code", ok, []) is expected

    async def test_예외는_가린_failed_단계로(self) -> None:
        async def boom() -> StageResult:
            raise RuntimeError(f"토큰 {SECRET} 실패\n다음 줄")

        result = await safe_stage("ingest", boom, [SECRET])
        assert result.kind == "ingest"
        assert result.result == "failed"
        assert result.error is not None
        assert SECRET not in result.error
        assert "\n" not in result.error
        assert result.summary == f"문서: 실패 — {result.error}"

    async def test_메시지가_비면_예외_이름(self) -> None:
        async def boom() -> StageResult:
            raise ValueError

        result = await safe_stage("code", boom, [])
        assert result.error == "ValueError"
        assert result.summary == "코드: 실패 — ValueError"

    async def test_메시지의_서버_경로를_가린다(self) -> None:
        async def boom() -> StageResult:
            raise RuntimeError("열 수 없음 /srv/team-wiki/state/x.json")

        result = await safe_stage("code", boom, [])
        assert result.error is not None
        assert "/srv/team-wiki" not in result.error

    async def test_취소는_전파한다(self) -> None:
        async def cancelled() -> StageResult:
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await safe_stage("code", cancelled, [])


# ---------------------------------------------------------------- run_once: 전체 흐름


class TestRunOnceFlow:
    async def test_PR을_만들고_대기_중엔_건너뛰고_머지_뒤엔_측정한다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir, raw_files=RAW)
        deps = env.deps

        r1 = await run_once(deps)
        assert r1 is not None
        assert r1.run_id == RUN_ID
        assert [(s.kind, s.result) for s in r1.stages] == [("code", "pr"), ("ingest", "pr")]
        assert r1.warnings == ()
        assert r1.line.startswith(f"[sync {RUN_ID}] 코드: ")
        assert " · 문서: " in r1.line
        assert r1.line.endswith("비용 $0.02")
        assert env.notifier.sent == [(r1.line, False)]
        assert [(r.kind, r.result) for r in records(env)] == [("code", "pr"), ("ingest", "pr")]
        assert lock_is_free(env)

        env.set_now(DEFAULT_NOW + timedelta(hours=3))
        r2 = await run_once(deps)
        assert r2 is not None
        assert [s.result for s in r2.stages] == ["skipped-open-pr", "skipped-open-pr"]

        await env.merge_branch(f"sync/code-{RUN_ID}")
        env.set_now(DEFAULT_NOW + timedelta(hours=15))
        r3 = await run_once(deps)
        assert r3 is not None
        assert [(r.pr, r.outcome, r.human_changed_files) for r in r3.pr_reviews] == [
            (f"{RUN_ID}-code.md", "merged", 0)
        ]
        assert [(e.kind, e.title) for e in r3.oq_events] == [("opened", "[2026-10-07] 샘플 어긋남")]
        assert [s.result for s in r3.stages] == ["no-change", "skipped-open-pr"]
        assert len(records(env)) == 6

    async def test_only_ingest는_문서_단계만_돌리고_경계_정리_대기도_안_한다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = await make_sync_env(tmp_dir, raw_files=RAW)
        ran: list[str] = []

        async def fake_code(ctx: StageContext) -> StageResult:
            ran.append("code")
            return stage_of("code")

        async def fake_ingest(ctx: StageContext) -> StageResult:
            ran.append("ingest")
            return stage_of("ingest")

        monkeypatch.setattr(PATCH + "run_code_sync", fake_code)
        monkeypatch.setattr(PATCH + "run_ingest", fake_ingest)
        drain = Drain()
        report = await run_once(dataclasses.replace(env.deps, drain=drain), only="ingest")
        assert report is not None
        assert ran == ["ingest"]
        assert [s.kind for s in report.stages] == ["ingest"]
        assert [r.kind for r in records(env)] == ["ingest"]
        assert drain.calls == []

    async def test_only_ingest는_실제_문서_단계로_PR을_만든다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir, raw_files=RAW)
        report = await run_once(env.deps, only="ingest")
        assert report is not None
        assert [(s.kind, s.result) for s in report.stages] == [("ingest", "pr")]

    async def test_only_code는_코드_단계만(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = await make_sync_env(tmp_dir, raw_files=RAW)
        ran: list[str] = []

        async def fake_code(ctx: StageContext) -> StageResult:
            ran.append("code")
            return stage_of("code")

        async def fake_ingest(ctx: StageContext) -> StageResult:
            ran.append("ingest")
            return stage_of("ingest")

        monkeypatch.setattr(PATCH + "run_code_sync", fake_code)
        monkeypatch.setattr(PATCH + "run_ingest", fake_ingest)
        drain = Drain()
        report = await run_once(dataclasses.replace(env.deps, drain=drain), only="code")
        assert report is not None
        assert ran == ["code"]
        assert [s.kind for s in report.stages] == ["code"]
        assert drain.calls == []

    async def test_알_수_없는_only는_잠금을_잡기_전에_거부한다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        with pytest.raises(ValueError):
            await run_once(env.deps, only="both")  # type: ignore[arg-type]
        assert env.notifier.sent == []
        assert lock_is_free(env)

    async def test_실패한_단계가_있으면_관리자를_멘션한다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        env.runner.set_script(lambda inp, call: {"ok": False, "failure": "timeout"})
        report = await run_once(env.deps, only="code")
        assert report is not None
        assert report.stages[0].result == "failed"
        assert [mention for _, mention in env.notifier.sent] == [True]
        assert [r.result for r in records(env)] == ["failed"]

    async def test_성공_보고는_멘션하지_않는다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        await run_once(env.deps, only="code")
        assert [mention for _, mention in env.notifier.sent] == [False]

    async def test_기본_now는_deps_now를_쓴다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        env.set_now(DEFAULT_NOW + timedelta(minutes=30, hours=12))
        report = await run_once(env.deps, only="code")
        assert report is not None
        assert report.run_id == "20261007-2130"
        assert records(env)[0].at == "2026-10-07T21:30:00+09:00"


# ---------------------------------------------------------------- 정리 대기(G1, 이월 1)


class TestDrainBetweenStages:
    async def test_코드_단계_뒤_남은_정리가_있으면_문서_단계를_돌리지_않는다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = await make_sync_env(tmp_dir, raw_files=RAW)
        code_ran = False

        async def fake_code(ctx: StageContext) -> StageResult:
            nonlocal code_ran
            code_ran = True
            return stage_of("code", summary="코드: 변경 1건 → x")

        calls: list[bool] = []

        async def drain(timeout: float) -> int:
            calls.append(code_ran)
            return 1 if code_ran else 0

        monkeypatch.setattr(PATCH + "run_code_sync", fake_code)
        deps = dataclasses.replace(env.deps, drain=drain)
        report = await run_once(deps)
        assert report is not None
        code, ingest = report.stages
        assert code.result == "pr"
        assert ingest.kind == "ingest"
        assert ingest.result == "failed"
        assert ingest.error == UNSETTLED
        assert ingest.summary == f"문서: 실패 — {UNSETTLED}"
        assert env.runner.inputs == []  # 문서용 Claude는 불리지 않았다
        assert calls == [True]  # 경계에서 정확히 한 번, 코드 단계가 끝난 뒤에
        assert [r.result for r in records(env)] == ["pr", "failed"]
        assert [mention for _, mention in env.notifier.sent] == [True]
        assert lock_is_free(env)

    async def test_정리가_끝났으면_문서_단계가_돈다_경계에서_한_번만_부른다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = await make_sync_env(tmp_dir, raw_files=RAW)

        async def fake_code(ctx: StageContext) -> StageResult:
            return stage_of("code")

        ingest_ran = False
        real_ingest = ingest_module.run_ingest

        async def spy_ingest(ctx: StageContext) -> StageResult:
            nonlocal ingest_ran
            ingest_ran = True
            return await real_ingest(ctx)

        seen_before_ingest: list[bool] = []
        pending = Drain(0)

        async def drain(timeout: float) -> int:
            seen_before_ingest.append(ingest_ran)
            return await pending(timeout)

        monkeypatch.setattr(PATCH + "run_code_sync", fake_code)
        monkeypatch.setattr(PATCH + "run_ingest", spy_ingest)
        report = await run_once(dataclasses.replace(env.deps, drain=drain, settle_timeout_s=7.0))
        assert report is not None
        assert [s.result for s in report.stages] == ["pr", "pr"]
        # 문서 단계 안의 Claude 실행마다 deps.drain이 따로 불린다. 경계의 부름은 문서 단계 전 한 번.
        assert seen_before_ingest[0] is False
        assert seen_before_ingest.count(False) == 1
        assert pending.calls[0] == 7.0

    async def test_정리_대기가_예외를_던지면_정리_안_된_것으로_본다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = await make_sync_env(tmp_dir, raw_files=RAW)

        async def fake_code(ctx: StageContext) -> StageResult:
            return stage_of("code")

        async def drain(timeout: float) -> int:
            raise RuntimeError(f"drain 실패 {SECRET}")

        monkeypatch.setattr(PATCH + "run_code_sync", fake_code)
        report = await run_once(dataclasses.replace(env.deps, drain=drain, redact=(SECRET,)))
        assert report is not None
        assert report.stages[1].result == "failed"
        assert report.stages[1].error == UNSETTLED
        assert env.runner.inputs == []
        assert SECRET not in report.line


# ---------------------------------------------------------------- 단계 실패 격리


class TestStageIsolation:
    async def test_코드_단계가_예외를_던져도_문서_단계는_진행한다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = await make_sync_env(tmp_dir, raw_files=RAW)

        async def boom(ctx: StageContext) -> StageResult:
            raise RuntimeError(f"코드 실패 {SECRET} /srv/team-wiki/repos/worker/x")

        monkeypatch.setattr(PATCH + "run_code_sync", boom)
        deps = dataclasses.replace(env.deps, redact=(SECRET,))
        report = await run_once(deps)
        assert report is not None
        code, ingest = report.stages
        assert code.result == "failed"
        assert code.error is not None
        assert SECRET not in code.error
        assert "/srv/team-wiki" not in code.error
        assert ingest.result == "pr"  # 스펙 §7.2: 3이 실패해도 4는 진행
        assert SECRET not in report.line
        assert all(SECRET not in text for text, _ in env.notifier.sent)
        assert [mention for _, mention in env.notifier.sent] == [True]
        assert [r.result for r in records(env)] == ["failed", "pr"]

    async def test_문서_단계가_예외를_던져도_앞_단계_기록은_남는다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = await make_sync_env(tmp_dir, raw_files=RAW)

        async def boom(ctx: StageContext) -> StageResult:
            raise OSError("디스크 오류")

        monkeypatch.setattr(PATCH + "run_ingest", boom)
        report = await run_once(env.deps)
        assert report is not None
        assert [s.result for s in report.stages] == ["pr", "failed"]
        assert [r.kind for r in records(env)] == ["code", "ingest"]

    async def test_단계에는_이_실행의_run_id_시각_잠금이_간다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = await make_sync_env(tmp_dir)
        contexts: list[StageContext] = []

        async def spy(ctx: StageContext) -> StageResult:
            contexts.append(ctx)
            assert ctx.lock.held
            return stage_of("code" if len(contexts) == 1 else "ingest", "no-change")

        monkeypatch.setattr(PATCH + "run_code_sync", spy)
        monkeypatch.setattr(PATCH + "run_ingest", spy)
        await run_once(env.deps)
        assert len(contexts) == 2
        assert {c.run_id for c in contexts} == {RUN_ID}
        assert {c.now for c in contexts} == {DEFAULT_NOW}
        assert all(c.deps is env.deps for c in contexts)
        assert not contexts[0].lock.held  # 실행이 끝나면 놓였다


# ---------------------------------------------------------------- 측정 실패 → 경고


class TestMeasurementWarnings:
    async def test_미결_추적이_예외면_경고로_남기고_단계는_진행한다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = await make_sync_env(tmp_dir)

        def boom(*args: object, **kwargs: object) -> list[object]:
            raise RuntimeError(f"추적 실패 {SECRET} /srv/team-wiki/state/oq.json")

        monkeypatch.setattr(PATCH + "track_open_questions", boom)
        report = await run_once(dataclasses.replace(env.deps, redact=(SECRET,)), only="code")
        assert report is not None
        assert report.stages[0].result in ("pr", "no-change", "direct")
        assert len(report.warnings) == 1
        warning = report.warnings[0]
        assert warning.startswith("측정 실패: ")
        assert SECRET not in warning
        assert "/srv/team-wiki" not in warning
        assert report.line.endswith(" · 경고 1건")
        assert report.oq_events == ()

    async def test_PR_결과_수집이_GitError면_경고만_남기고_계속한다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = await make_sync_env(tmp_dir)

        async def boom(**kwargs: object) -> list[object]:
            raise GitError(f"fetch 실패 {SECRET}", kind="exit", code=128)

        monkeypatch.setattr(PATCH + "collect_pr_reviews", boom)
        report = await run_once(dataclasses.replace(env.deps, redact=(SECRET,)), only="code")
        assert report is not None
        assert [w.startswith("측정 실패: ") for w in report.warnings] == [True]
        assert SECRET not in report.warnings[0]
        assert report.pr_reviews == ()
        assert len(report.stages) == 1

    async def test_두_측정이_모두_실패하면_경고가_둘이다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = await make_sync_env(tmp_dir)

        def oq_boom(*args: object, **kwargs: object) -> list[object]:
            raise OSError("읽기 실패")

        async def pr_boom(**kwargs: object) -> list[object]:
            raise ValueError

        monkeypatch.setattr(PATCH + "track_open_questions", oq_boom)
        monkeypatch.setattr(PATCH + "collect_pr_reviews", pr_boom)
        report = await run_once(env.deps, only="code")
        assert report is not None
        assert len(report.warnings) == 2
        assert report.line.endswith(" · 경고 2건")

    async def test_포트가_없으면_PR_측정은_건너뛴다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = await make_sync_env(tmp_dir, review_mode="direct")
        assert env.deps.pr_port is None

        async def forbidden(**kwargs: object) -> list[object]:
            raise AssertionError("포트가 없으면 부르지 않는다")

        monkeypatch.setattr(PATCH + "collect_pr_reviews", forbidden)
        report = await run_once(env.deps, only="code")
        assert report is not None
        assert report.warnings == ()

    async def test_기록_쓰기_실패는_경고이고_실행은_끝까지_간다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        # 상태 파일 자리에 폴더가 있으면 append가 OSError를 던진다.
        (env.paths.state_dir / STATE_FILES.sync_runs).mkdir(parents=True)
        report = await run_once(env.deps, only="code")
        assert report is not None
        assert len(report.warnings) == 1
        assert report.warnings[0].startswith("기록 실패: ")
        assert env.notifier.sent[0][0] == report.line
        assert report.line.endswith(" · 경고 1건")
        assert lock_is_free(env)

    async def test_보고_전송이_예외를_던져도_결과는_돌려준다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)

        class Broken:
            async def send(self, text: str, *, mention_admin: bool = False) -> None:
                raise RuntimeError(f"전송 실패 {SECRET}")

        deps = dataclasses.replace(env.deps, notifier=Broken(), redact=(SECRET,))
        report = await run_once(deps, only="code")
        assert report is not None
        assert any(w.startswith("보고 전송 실패: ") for w in report.warnings)
        assert all(SECRET not in w for w in report.warnings)
        assert lock_is_free(env)


# ---------------------------------------------------------------- 잠금


class TestLock:
    async def test_이미_실행_중이면_멘션_없이_건너뛴다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        other = acquire_lock(env.paths.state_dir / STATE_FILES.lock, now=DEFAULT_NOW)
        assert other is not None
        try:
            assert await run_once(env.deps) is None
            assert env.notifier.sent == [(BUSY, False)]
            assert env.runner.inputs == []
            # 건너뛴 쪽이 쥔 쪽의 잠금 내용을 건드리지 않는다.
            owner = read_lock_owner(env.paths.state_dir / STATE_FILES.lock)
            assert owner.pid is not None
            assert records(env) == []
        finally:
            other.release()

    async def test_6시간_넘게_쥔_잠금이면_관리자를_멘션한다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        other = acquire_lock(
            env.paths.state_dir / STATE_FILES.lock, now=DEFAULT_NOW - timedelta(hours=7)
        )
        assert other is not None
        try:
            assert await run_once(env.deps) is None
            assert env.notifier.sent == [
                (f"{BUSY} — 잠금이 6시간 넘게 유지됨(pid {os.getpid()})", True)
            ]
        finally:
            other.release()

    async def test_6시간_안쪽이면_멘션하지_않는다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)
        other = acquire_lock(
            env.paths.state_dir / STATE_FILES.lock, now=DEFAULT_NOW - timedelta(hours=5)
        )
        assert other is not None
        try:
            assert await run_once(env.deps) is None
            assert env.notifier.sent == [(BUSY, False)]
        finally:
            other.release()

    async def test_건너뜀_알림이_실패해도_던지지_않는다(self, tmp_dir: Path) -> None:
        env = await make_sync_env(tmp_dir)

        class Broken:
            async def send(self, text: str, *, mention_admin: bool = False) -> None:
                raise RuntimeError("전송 실패")

        other = acquire_lock(env.paths.state_dir / STATE_FILES.lock, now=DEFAULT_NOW)
        assert other is not None
        try:
            assert await run_once(dataclasses.replace(env.deps, notifier=Broken())) is None
        finally:
            other.release()

    async def test_데이터_사본_준비가_실패하면_가린_SyncRunError_잠금은_놓인다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = await make_sync_env(tmp_dir)

        async def boom(*args: object, **kwargs: object) -> None:
            raise GitError(f"clone 실패 {SECRET} /srv/team-wiki/data-sync/x", kind="exit")

        monkeypatch.setattr(PATCH + "prepare_data_copy", boom)
        deps = dataclasses.replace(env.deps, redact=(SECRET,))
        with pytest.raises(SyncRunError) as info:
            await run_once(deps)
        assert SECRET not in str(info.value)
        assert "/srv/team-wiki" not in str(info.value)
        assert str(info.value).startswith("데이터 사본 준비 실패: ")
        assert info.value.__cause__ is None
        assert info.value.__suppress_context__
        assert lock_is_free(env)
        assert env.notifier.sent == []  # 보고는 main의 몫이다
        assert env.runner.inputs == []

    async def test_데이터_사본_준비에는_설정의_URL과_브랜치를_쓴다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = await make_sync_env(tmp_dir)
        seen: list[tuple[object, str, str]] = []

        async def spy(git: object, url: str, base: str) -> None:
            seen.append((git, url, base))

        monkeypatch.setattr(PATCH + "prepare_data_copy", spy)
        await run_once(env.deps, only="code")
        assert seen == [(env.deps.data_git, env.config.data_repo.url, "main")]

    async def test_취소되면_잠금을_놓고_전파한다_기록과_보고는_남기지_않는다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = await make_sync_env(tmp_dir, raw_files=RAW)
        started = asyncio.Event()

        async def hang(ctx: StageContext) -> StageResult:
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("도달하지 않는다")

        monkeypatch.setattr(PATCH + "run_code_sync", hang)
        task = asyncio.create_task(run_once(env.deps))
        async with asyncio.timeout(30):
            await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            async with asyncio.timeout(30):
                await task
        assert lock_is_free(env)
        assert records(env) == []
        assert env.notifier.sent == []

    async def test_정리_대기_중_취소도_잠금을_놓는다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = await make_sync_env(tmp_dir, raw_files=RAW)
        started = asyncio.Event()

        async def fake_code(ctx: StageContext) -> StageResult:
            return stage_of("code")

        async def hang_drain(timeout: float) -> int:
            started.set()
            await asyncio.Event().wait()
            return 0

        monkeypatch.setattr(PATCH + "run_code_sync", fake_code)
        task = asyncio.create_task(run_once(dataclasses.replace(env.deps, drain=hang_drain)))
        async with asyncio.timeout(30):
            await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            async with asyncio.timeout(30):
                await task
        assert lock_is_free(env)
        assert [r.kind for r in records(env)] == ["code"]  # 끝난 단계의 기록은 이미 남았다
        assert env.notifier.sent == []


# ---------------------------------------------------------------- 보고 내용 가림


class TestReportMasking:
    async def test_보고_줄의_비밀값과_경로를_가린다(
        self, tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = await make_sync_env(tmp_dir)

        async def leaky(ctx: StageContext) -> StageResult:
            return stage_of(
                "code", summary=f"코드: 변경 1건 {SECRET} {env.paths.repos_dir}/worker/x"
            )

        monkeypatch.setattr(PATCH + "run_code_sync", leaky)
        report = await run_once(dataclasses.replace(env.deps, redact=(SECRET,)), only="code")
        assert report is not None
        assert SECRET not in report.line
        assert str(env.paths.home) not in report.line
        assert env.notifier.sent == [(report.line, False)]


def test_SyncRunReport는_불변이다() -> None:
    report = SyncRunReport(
        run_id=RUN_ID, stages=(), pr_reviews=(), oq_events=(), warnings=(), line="x"
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        report.line = "y"  # type: ignore[misc]
