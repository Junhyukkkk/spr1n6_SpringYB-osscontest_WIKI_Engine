"""코드 반영 단계(스펙 §7.3, 계획 2 Task 14).

순서: 잠금 확인 → 열린 PR이면 건너뜀 → 작업 브랜치를 `origin/<base>`로 → 기준 읽기 → 코드 사본·시점
기록 → 저장소별 변경분 → (바뀐 것 없으면 끝) → Claude(`run_write_step`) → 커밋 → 기준 값 →
주간 요약 → 점검 루프 → 로그 → PR 본문·전달 → 보강 후보 소비·열린 PR 스냅샷.

규칙:
- **git 변경은 모두 `run_to_completion` 안에서 돈다**(`reset_branch_to`·`commit_all`). 취소가 와도
  git 프로세스 그룹을 죽이지 않아 `.git/*.lock`(특히 `HEAD.lock`)이 남지 않는다. Claude 실행 쪽의
  되돌리기는 `run_write_step`·`fail_stage`가 같은 보호를 한다.
- 스크립트의 쓰기(`apply_baseline`, 주간 요약, `append_log`) 앞에서 `wiki/`와 `wiki/log.md`가 링크가
  아닌지 다시 본다(Claude 쓰기 사전 검사는 `run_write_step`이 한다). 사후 검사는 항상 로그를 쓰기
  **전에** 돈다(G5).
- 운영 채널에 가는 모든 메시지는 `safe_message`(알려진 비밀값 가림·300자)를 지난다. 예외 메시지는
  `from None`으로 연쇄하지 않는다.
- 잠금이 없으면 아무것도 건드리지 않는다: 작업 사본을 되돌리지도(`fail_stage` 하지 않음) 쓰지도
  않는다 — 다른 실행이 쥔 `data-sync`를 지울 수 있기 때문이다. 잠금을 쥔 채로 나는 그 밖의 모든
  실패(`LockNotHeldError`를 `append_log_locked`에서 만난 경우 포함)는 `fail_stage`로 작업 사본을
  되돌린다. 두 경로가 다른 것은 의도다.
- 알려진 한계: 코드 사본 시점 기록(`write_code_snapshot`)은 이전 값에 합쳐 쓰므로 활성 저장소만
  남기도록 단계가 뒤에 한 번 더 걸러 쓴다.
"""

import dataclasses
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Final

from pydantic import FailFast, Field, TypeAdapter

from team_wiki_engine.claude.types import RunClaudeInput
from team_wiki_engine.config.schema import RepoConfig
from team_wiki_engine.git.client import GitClient
from team_wiki_engine.git.ops import (
    changed_paths,
    commit_all,
    commit_paths,
    reset_branch_to,
    try_rev_parse,
)
from team_wiki_engine.metrics.oq import question_headings
from team_wiki_engine.metrics.record import (
    STATE_FILES,
    GapRecord,
    mark_gaps_consumed,
    read_json,
    unconsumed_gaps,
    write_json,
)
from team_wiki_engine.metrics.weekly import WeeklyDue, weekly_due
from team_wiki_engine.ops.notify import redact_secrets
from team_wiki_engine.sync.claude_step import run_write_step
from team_wiki_engine.sync.delta import (
    DeltaSummary,
    QuestionGap,
    RepoDelta,
    compute_repo_delta,
    open_pr_keys,
)
from team_wiki_engine.sync.lint_loop import LintLoopResult, lint_loop
from team_wiki_engine.sync.pr import PrBodyInput, build_pr_body, deliver, extract_section
from team_wiki_engine.sync.prompts import code_sync_prompt
from team_wiki_engine.sync.repos import (
    CodeSnapshot,
    read_code_snapshot,
    sync_code_repos,
    write_code_snapshot,
)
from team_wiki_engine.sync.script_fields import apply_baseline, script_value_paths
from team_wiki_engine.sync.stage import (
    DIRECT_LINT_MESSAGE,
    EXPECTED_ERRORS,
    RUN_ID_PATTERN,
    LockNotHeldError,
    StageContext,
    StageError,
    append_log_locked,
    check_script_targets,
    describe_error,
    difference,
    ensure_clean_tree,
    fail_stage,
    fail_without_lock,
    guard,
    load_baseline,
    make_mask,
    path_roots,
    pr_port_warnings,
    scrub_paths,
    wiki_pages_changed,
    work_branch,
)
from team_wiki_engine.sync.types import StageResult, add_usage, new_stage
from team_wiki_engine.util.aio import run_to_completion
from team_wiki_engine.util.fs import read_text_if_exists, write_file_atomic
from team_wiki_engine.util.time import kst_iso, kst_parts
from team_wiki_engine.wiki.baseline import Baseline, RepoBaseline
from team_wiki_engine.wiki.log import LogEntry

WEEKLY_ONLY_SECTION: Final = "- 없음 (주간 요약만 반영)"
EMPTY_SUMMARY: Final = "코드: 변경 없음 (Claude 응답 없음)"
EMPTY_WARNING: Final = (
    "Claude가 응답을 내지 않았고 바뀐 파일도 없어 이번 범위를 반영하지 않음 — 다음 실행이 다시 함"
)
MAX_GAPS: Final = 20
MAX_OPEN_PR_KEYS: Final = 1000

_OPEN_PRS_ADAPTER: Final[TypeAdapter[dict[str, list[str]]]] = TypeAdapter(
    dict[str, Annotated[list[str], FailFast(), Field(max_length=MAX_OPEN_PR_KEYS)]]
)
_LOW_CONFIDENCE_HEADING: Final = "## 확신이 낮은 부분"


# ---------------------------------------------------------------- 문구


def _describe_targets(deltas: Sequence[RepoDelta], weekly_label: str | None) -> str:
    parts: list[str] = []
    for d in deltas:
        items = [f"#{p.number}" for p in d.merged_pull_requests]
        if d.direct_commits:
            items.append(f"직접 커밋 {len(d.direct_commits)}")
        if items:
            parts.append(f"{d.name} {' '.join(items)}")
    if weekly_label is not None:
        parts.append(f"주간 요약 {weekly_label}")
    return ", ".join(parts) if parts else "열린 PR·질문 반영"


def _describe_reflected(
    deltas: Sequence[RepoDelta], gap_count: int, archived: Sequence[str], weekly_label: str | None
) -> list[str]:
    lines: list[str] = []
    for d in deltas:
        lines.extend(f"{d.name} #{p.number} {p.title} ({p.author})" for p in d.merged_pull_requests)
        lines.extend(f"{d.name} {c.sha[:7]} {c.subject} ({c.author})" for c in d.direct_commits)
    if gap_count > 0:
        lines.append(f"팀원 질문 {gap_count}건 보강 검토")
    lines.extend(f"{name} 보관 처리" for name in archived)
    if weekly_label is not None:
        lines.append(f"주간 측정 요약 {weekly_label}")
    return lines


def _next_baseline(
    baseline: Baseline, deltas: Sequence[RepoDelta], archived: Sequence[str], now: datetime
) -> Baseline:
    """다음 기준. 기존 객체는 바꾸지 않는다(새 `RepoBaseline`을 만든다)."""
    repos = {name: RepoBaseline(r.commit, r.archived) for name, r in baseline.repos.items()}
    for d in deltas:
        repos[d.name] = RepoBaseline(commit=d.to, archived=False)
    for name in archived:
        previous = repos.get(name)
        if previous is not None:
            repos[name] = RepoBaseline(commit=previous.commit, archived=True)
    return Baseline(
        repos=repos, raw_processed=list(baseline.raw_processed), updated_at=kst_iso(now)
    )


def _prune_snapshot(state_dir: Path, active: set[str]) -> None:
    """코드 사본 시점 기록에서 활성이 아닌(설정에서 빠지거나 보관된) 저장소를 뺀다."""
    snapshot = read_code_snapshot(state_dir)
    if snapshot is None or set(snapshot.repos) <= active:
        return
    kept = {name: sha for name, sha in snapshot.repos.items() if name in active}
    write_json(
        state_dir / STATE_FILES.code_snapshot,
        CodeSnapshot(fetched_at=snapshot.fetched_at, repos=kept),
    )


# ---------------------------------------------------------------- 단계


async def run_code_sync(ctx: StageContext) -> StageResult:
    stage = new_stage("code")
    if not ctx.lock.held:  # G11, S5: 잠금 없이는 Claude도 git도 부르지 않는다
        return fail_without_lock(stage)
    try:
        return await _run(ctx, stage)
    except StageError as exc:
        return await fail_stage(stage, ctx.deps.data_git, str(exc))
    except LockNotHeldError as exc:  # 고정 문구라 안전하다
        return await fail_stage(stage, ctx.deps.data_git, str(exc))


@dataclass(frozen=True)
class _Plan:
    """읽기 단계(1~5)의 결과: 이번 실행이 무엇을 반영할지."""

    branch: str
    run_dir: Path
    baseline: Baseline
    deltas: list[RepoDelta]
    archived_repos: list[str]
    newly_archived: list[str]
    gaps: list[GapRecord]
    open_keys: dict[str, list[str]]
    weekly: WeeklyDue | None
    needs_claude: bool
    change_count: int

    @property
    def weekly_label(self) -> str | None:
        return None if self.weekly is None else self.weekly.label


@dataclass(frozen=True)
class _ClaudeOutcome:
    low_confidence: str | None
    session_id: str | None


async def _run(ctx: StageContext, stage: StageResult) -> StageResult:
    deps = ctx.deps
    config, paths = deps.config, deps.paths
    if RUN_ID_PATTERN.fullmatch(ctx.run_id) is None:
        raise StageError("run_id 형식이 올바르지 않습니다")
    if config.review_mode == "pr" and deps.pr_port is None:  # Claude 비용을 쓰기 전에 막는다
        raise StageError("검토 방식이 pr인데 PR 포트가 없습니다")

    plan = await _plan(ctx, stage)
    if isinstance(plan, StageResult):
        return plan
    questions_before = question_headings(
        read_text_if_exists(paths.data_sync_dir / "wiki" / "open-questions.md")
    )
    inp = RunClaudeInput(
        profile="sync-code",
        prompt="",
        cwd=paths.data_sync_dir,
        read_roots=(paths.repos_dir, plan.run_dir),
        write_roots=(paths.data_sync_dir / "wiki",),
        timeout_ms=config.limits.sync_timeout_ms,
    )
    claude = _ClaudeOutcome(WEEKLY_ONLY_SECTION, None)
    if plan.needs_claude:
        outcome = await _claude_and_commit(ctx, stage, plan, inp)
        if isinstance(outcome, StageResult):
            return outcome
        claude = outcome
    loop = await _script_writes(ctx, stage, plan, inp, claude.session_id)
    if isinstance(loop, StageResult):
        return loop
    pages = await _write_log(ctx, stage, plan)
    return await _deliver(ctx, stage, plan, claude, loop, pages, questions_before)


async def _plan(ctx: StageContext, stage: StageResult) -> _Plan | StageResult:
    """1~5: 열린 PR 확인 → 브랜치 → 기준 → 코드 사본 → 변경분. 끝낼 이유가 있으면 그 결과."""
    deps = ctx.deps
    config, paths, git = deps.config, deps.paths, deps.data_git
    redact = deps.redact
    mask = make_mask(deps)
    base = config.data_repo.branch
    data_dir = paths.data_sync_dir
    now = ctx.now
    branch = work_branch("code", ctx.run_id)

    # 1. 같은 종류의 열린 PR이면 건너뛴다.
    if config.review_mode == "pr" and deps.pr_port is not None:
        with guard(mask, "열린 PR을 확인할 수 없습니다"):
            open_id = await deps.pr_port.find_open("code")
        if open_id is not None:
            stage.result = "skipped-open-pr"
            stage.summary = f"코드: 검토 대기 중({mask(open_id)})"
            return stage

    # 2. 작업 브랜치를 origin/<base>에서 새로 만든다.
    with guard(mask, "작업 브랜치를 만들 수 없습니다"):
        await run_to_completion(reset_branch_to(git, branch, f"origin/{base}"))

    # 3. 기준 읽기(G8).
    baseline = load_baseline(data_dir, redact)

    # 4. 코드 사본을 받고 시점을 기록한다.
    active = [r for r in config.repos if r.status == "active"]
    archived_repos = [r.name for r in config.repos if r.status == "archived"]
    with guard(mask, "코드 사본을 받을 수 없습니다"):
        synced = await sync_code_repos(repos=active, repos_dir=paths.repos_dir, git=deps.code_git)
        write_code_snapshot(paths.state_dir, synced.heads, now)
        _prune_snapshot(paths.state_dir, {r.name for r in active})
    for name, message in synced.failures.items():
        reason = mask(message)
        stage.warnings.append(f"{name}: 받아 오지 못해 이번 범위에서 뺌 ({reason})")

    # 5. 저장소별 변경분. 읽기 루트(`runs/<run_id>`)는 diff가 없어도 항상 만든다.
    run_dir = paths.runs_dir / ctx.run_id
    deltas: list[RepoDelta] = []
    with guard(mask, "변경분을 만들 수 없습니다"):
        run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        for repo in active:
            if repo.name not in synced.heads:
                continue
            deltas.append(await _repo_delta(ctx, stage, repo, baseline, run_dir))

    newly_archived = [
        name
        for name in archived_repos
        if (previous := baseline.repos.get(name)) is not None and not previous.archived
    ]
    gaps = unconsumed_gaps(paths.state_dir, MAX_GAPS)
    previous_keys = read_json(paths.state_dir / STATE_FILES.open_pr_snapshot, _OPEN_PRS_ADAPTER, {})
    active_names = {r.name for r in active}
    # 이번에 받지 못한 활성 저장소의 지난 값은 이어 둔다(일시 실패가 "열린 PR 변화"가 되지 않게).
    open_keys = {
        **{n: keys for n, keys in previous_keys.items() if n in active_names},
        **open_pr_keys(deltas),
    }
    change_count = sum(len(d.merged_pull_requests) + len(d.direct_commits) for d in deltas)
    weekly = weekly_due(data_dir, paths.state_dir, now)
    needs_claude = (
        change_count > 0 or open_keys != previous_keys or bool(gaps) or bool(newly_archived)
    )
    if not needs_claude and weekly is None:
        stage.summary = "코드: 변경 없음"
        return stage
    return _Plan(
        branch=branch,
        run_dir=run_dir,
        baseline=baseline,
        deltas=deltas,
        archived_repos=archived_repos,
        newly_archived=newly_archived,
        gaps=gaps,
        open_keys=open_keys,
        weekly=weekly,
        needs_claude=needs_claude,
        change_count=change_count,
    )


async def _claude_and_commit(
    ctx: StageContext, stage: StageResult, plan: _Plan, inp: RunClaudeInput
) -> _ClaudeOutcome | StageResult:
    """6: Claude 실행(쓰기 루트 사전 검사 → 실행 → 정리 대기 → 사후 검사) → 커밋."""
    deps = ctx.deps
    git, redact, mask = deps.data_git, deps.redact, make_mask(deps)
    summary = DeltaSummary(
        run_id=ctx.run_id,
        repos=plan.deltas,
        archived_repos=plan.archived_repos,
        question_gaps=[
            QuestionGap(
                asked_at=g.at,
                question=g.question,
                answer_summary=g.answer_summary,
                code_paths=g.code_paths,
            )
            for g in plan.gaps
        ],
    )
    prompt = code_sync_prompt(summary, kst_parts(ctx.now).date)
    # `run_write_step`이 던지면(사후 검사의 git 오류 등) 실행 결과를 받지 못해 사용량을 더할 수
    # 없다. 그 경우에도 단계는 가린 메시지로 실패한다(작업 사본은 `fail_stage`가 되돌린다).
    with guard(mask, "Claude 실행을 검사할 수 없습니다"):
        step = await run_write_step(
            runner=deps.runner,
            inp=dataclasses.replace(inp, prompt=prompt),
            git=git,
            data_dir=deps.paths.data_sync_dir,
            drain=deps.drain,
            settle_timeout_s=deps.settle_timeout_s,
            redact=redact,
        )
    if step.run is not None:
        add_usage(stage, step.run)
    if step.status != "ok" or step.run is None:
        return await fail_stage(stage, git, mask(step.message))
    result = step.run
    if result.failure == "empty":  # 스펙 §7.9, G21
        with guard(mask, "변경 여부를 확인할 수 없습니다"):
            changed = await run_to_completion(changed_paths(git))
        if not changed:
            stage.result = "no-change"
            stage.warnings.append(EMPTY_WARNING)
            stage.summary = EMPTY_SUMMARY
            return stage  # baseline을 올리지 않아 다음 실행이 같은 범위를 다시 한다
    with guard(mask, "커밋할 수 없습니다"):
        await run_to_completion(commit_all(git, f"wiki: 코드 변경 반영 {ctx.run_id}"))
    low_confidence = extract_section(result.text, _LOW_CONFIDENCE_HEADING)
    if low_confidence is not None:  # Claude가 읽은 서버 경로가 PR 본문에 새지 않게 한다
        low_confidence = scrub_paths(
            redact_secrets(low_confidence, deps.redact), path_roots(deps.paths), generic=False
        )
    return _ClaudeOutcome(low_confidence, result.session_id)


async def _script_writes(
    ctx: StageContext,
    stage: StageResult,
    plan: _Plan,
    inp: RunClaudeInput,
    session_id: str | None,
) -> LintLoopResult | StageResult:
    """7~9: 기준 값 → 주간 요약 → 점검 루프(각각 별도 커밋)."""
    deps = ctx.deps
    git, redact, data_dir = deps.data_git, deps.redact, deps.paths.data_sync_dir
    mask = make_mask(deps)
    run_id = ctx.run_id

    # 7. 기준 값(G10): baseline.json과 현황 페이지를 점검 전에 맞춘다.
    next_baseline = _next_baseline(plan.baseline, plan.deltas, plan.archived_repos, ctx.now)

    def reapply() -> None:
        check_script_targets(data_dir, dirs=("wiki", "wiki/status"))
        apply_baseline(data_dir, next_baseline)

    await ensure_clean_tree(git, mask)  # Claude 실행이 앞서지 않았을 수도 있다(주간 요약만)
    with guard(mask, "기준 값을 쓸 수 없습니다"):
        reapply()
        paths = script_value_paths(data_dir)
        if paths:  # 스크립트가 쓰는 파일만 커밋한다
            await run_to_completion(commit_paths(git, paths, f"chore: 기준 커밋 갱신 {run_id}"))

    # 8. 주간 요약.
    if plan.weekly is not None:
        weekly = plan.weekly
        with guard(mask, "주간 요약을 쓸 수 없습니다"):
            check_script_targets(data_dir, dirs=("metrics", "metrics/weekly"))
            write_file_atomic(data_dir / weekly.rel_path, weekly.content)
            await run_to_completion(
                commit_paths(git, [weekly.rel_path], f"chore: 주간 측정 요약 {weekly.label}")
            )

    # 9. 점검 루프(G9: 점검을 할 수 없으면 단계 실패).
    with guard(mask, "점검 중 오류가 났습니다"):
        loop = await lint_loop(
            data_dir=data_dir,
            git=git,
            runner=deps.runner,
            inp=inp,
            session_id=session_id,
            reapply=reapply,
            fix_commit_message=f"wiki: 점검 위반 수정 {run_id}",
            today=kst_parts(ctx.now).date,
            drain=deps.drain,
            settle_timeout_s=deps.settle_timeout_s,
            redact=redact,
        )
    for run in loop.runs:
        add_usage(stage, run)
    if loop.failure is not None:
        return await fail_stage(stage, git, mask(loop.failure))
    stage.lint_failed = bool(loop.violations)
    return loop


async def _write_log(ctx: StageContext, stage: StageResult, plan: _Plan) -> list[str]:
    """10: 반영 이력(사후 검사 뒤, 잠금을 쥔 채로). 바뀐 페이지 목록을 돌려준다."""
    deps = ctx.deps
    git, data_dir = deps.data_git, deps.paths.data_sync_dir
    with guard(make_mask(deps), "반영 이력을 쓸 수 없습니다"):
        pages = await wiki_pages_changed(git, deps.config.data_repo.branch)
        check_script_targets(data_dir, dirs=("wiki",), files=("wiki/log.md",))
        entry = LogEntry(
            at=ctx.now,
            kind="code",
            targets=_describe_targets(plan.deltas, plan.weekly_label),
            pages=pages,
            cost_usd=stage.cost_usd,
        )
        try:
            append_log_locked(ctx, entry)
        except LockNotHeldError as exc:
            raise StageError(str(exc)) from None
        await run_to_completion(
            commit_paths(git, ["wiki/log.md"], f"chore: 반영 이력 {ctx.run_id}")
        )
    return pages


async def _deliver(
    ctx: StageContext,
    stage: StageResult,
    plan: _Plan,
    claude: _ClaudeOutcome,
    loop: LintLoopResult,
    pages: list[str],
    questions_before: list[str],
) -> StageResult:
    """11: PR 본문·전달 → 보강 후보 소비·열린 PR 스냅샷."""
    deps = ctx.deps
    config, paths, mask = deps.config, deps.paths, make_mask(deps)
    run_id = ctx.run_id
    questions_after = question_headings(
        read_text_if_exists(paths.data_sync_dir / "wiki" / "open-questions.md")
    )
    port_warnings = pr_port_warnings(deps.pr_port)
    body = build_pr_body(
        PrBodyInput(
            reflected=_describe_reflected(
                plan.deltas, len(plan.gaps), plan.newly_archived, plan.weekly_label
            ),
            changed_pages=[f"wiki/{p}" for p in pages],
            new_questions=difference(questions_after, questions_before),
            resolved_questions=difference(questions_before, questions_after),
            analyses=[],
            low_confidence=claude.low_confidence,
            lint_violations=loop.violations,
            warnings=[*stage.warnings, *port_warnings],
            run_id=run_id,
            cost_usd=stage.cost_usd,
            input_tokens=stage.input_tokens,
            output_tokens=stage.output_tokens,
        ),
        config.limits.pr_body_chars,
    )
    if config.review_mode == "direct" and stage.lint_failed:
        raise StageError(DIRECT_LINT_MESSAGE)
    with guard(mask, "전달에 실패했습니다"):
        delivered = await deliver(
            git=deps.data_git,
            kind="code",
            run_id=run_id,
            branch=plan.branch,
            base=config.data_repo.branch,
            title=f"위키 코드 반영 {run_id}",
            body=body,
            lint_failed=stage.lint_failed,
            review_mode=config.review_mode,
            pr_port=deps.pr_port,
            state_dir=paths.state_dir,
        )
    # 전달 중에 포트가 새로 남긴 경고(라벨 달기 실패 등)는 운영 보고용으로 단계 경고에 옮긴다.
    stage.warnings.extend(w for w in pr_port_warnings(deps.pr_port) if w not in port_warnings)
    stage.result = delivered.result
    stage.pr_id = delivered.pr_id

    # 전달이 성공한 뒤에만 보강 후보 소비와 열린 PR 스냅샷을 남긴다. 기록 실패는 이미 열린 PR을
    # 실패로 되돌릴 수 없으므로 경고로 남긴다(다음 실행은 열린 PR을 보고 건너뛴다).
    try:
        mark_gaps_consumed(paths.state_dir, [g.id for g in plan.gaps], run_id, ctx.now)
        write_json(paths.state_dir / STATE_FILES.open_pr_snapshot, plan.open_keys)
    except EXPECTED_ERRORS as exc:
        stage.warnings.append(f"상태 기록 실패: {describe_error(exc, mask)}")
    target = delivered.pr_id or "main 직접 반영"
    suffix = f" (점검 실패 {len(loop.violations)}건)" if stage.lint_failed else ""
    stage.summary = f"코드: 변경 {plan.change_count}건 → {target}{suffix}"
    return stage


async def _repo_delta(
    ctx: StageContext,
    stage: StageResult,
    repo: RepoConfig,
    baseline: Baseline,
    run_dir: Path,
) -> RepoDelta:
    """저장소 하나의 변경분. 기준 커밋이 사라졌으면 경고하고 처음부터 읽는다(Review Focus 2).

    그 밖의 git 오류는 호출자의 `_guard`가 단계 실패로 바꾼다.
    """
    deps = ctx.deps
    git: GitClient = deps.code_git.at(deps.paths.repos_dir / repo.name)
    recorded = baseline.repos.get(repo.name)
    from_ = None if recorded is None else recorded.commit
    if from_ is not None and await try_rev_parse(git, from_) is None:
        stage.warnings.append(
            f"{repo.name}: 기준 커밋 {from_[:7]}을 찾을 수 없어 처음부터 다시 읽음"
        )
        from_ = None
    result = await compute_repo_delta(
        repo=repo,
        git=git,
        from_=from_,
        github=deps.github,
        run_dir=run_dir,
        diff_limit=deps.config.limits.diff_bytes_per_repo,
        now=ctx.now,
    )
    mask = make_mask(deps)
    stage.warnings.extend(mask(w) for w in result.warnings)
    return result.delta
