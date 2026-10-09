"""문서 반영 단계(스펙 §7.5~§7.9, 계획 2 Task 15).

순서: 잠금 확인 → 열린 PR이면 건너뜀 → 작업 브랜치를 `origin/<base>`로 → 기준 읽기 → `raw/`의 새
원본 고르기 → 원본마다(Claude `run_write_step` → 커밋 → 기준에 처리 기록 → 커밋) → 재확인 →
점검 루프 → 로그 → PR 본문·전달.

규칙(코드 반영 단계 `code_sync.py`와 같다):
- **git 변경은 모두 `run_to_completion` 안에서 돈다**(`reset_branch_to`·`commit_all`·
  `commit_paths`). 스크립트가 쓴 파일(`baseline.json`, `wiki/log.md`)의 커밋은 `commit_paths`로
  그 경로만 담는다 — 다른 바뀐 파일(예: 목록을 만든 뒤 바뀐 `raw/`)이 섞여 들어가지 않는다.
  Claude가 쓴 `wiki/` 변경만 `commit_all`로 커밋한다(사후 검사를 이미 지났다). 취소가 와도
  git 프로세스 그룹을 죽이지 않아 `.git/*.lock`이 남지 않는다. 취소(`CancelledError`)는 어디서도
  실패로 바꾸지 않는다.
- 스크립트의 쓰기(`write_baseline`, `apply_baseline`, `append_log`) 앞에서 `wiki/`·`wiki/log.md`·
  `baseline.json`이 링크가 아닌지 다시 본다. 사후 검사는 `run_write_step`이 Claude 실행마다 하므로
  항상 로그를 쓰기 **전에** 돈다(G5). Claude는 커밋된 깨끗한 작업 사본에서만 쓴다.
- 운영 채널·PR 본문에 가는 모든 글은 `Mask`(비밀값 가림 → 절대 경로 바꾸기 → 자르기)를 지난다.
  원본 파일 이름은 공격자가 정할 수 있다(`raw/`에는 팀원이 푸시한다): 이름은 `classify_raw_names`
  (G14)를 통과한 것만 쓰고(비밀값처럼 보이거나 알려진 비밀값이 든 이름은 반영하지 않는다 —
  로그·PR·페이지 이름으로 새지 않게), 프롬프트를 만들기 직전에 한 번 더 확인하며, 글에는
  `prompt_name`/`clean`을 거친 꼴로만 싣는다. 표시용 이름은 **전체를 가린 뒤** 80자로 자른다.
- **상태는 전달이 성공해야 앞선다.** `raw_processed`는 작업 브랜치의 `chore: … 처리 기록` 커밋으로만
  바뀌고, 그 브랜치는 `deliver`가 push할 때에야 `origin`에 간다. 전달이 실패하면 `main`의 기준은
  그대로라 다음 실행이 같은 원본부터 다시 한다.
- 잠금을 잃은 경우는 두 갈래다. 시작할 때 잠금이 없거나 단계 도중(`_require_lock`) 잃은 것을
  알아차리면 `fail_without_lock`으로 **작업 사본을 건드리지 않고** 끝낸다(다른 실행이 쥔 사본일
  수 있다). 로그를 쓰는 곳(`append_log_locked`)에서야 알아차린 `LockNotHeldError`는 코드 반영
  단계와 같이 `StageError`가 돼 `fail_stage`가 되돌린다(쓰기 전에 거부하고 문구는 고정이다).
  그 밖에 잠금을 쥔 채로 나는 실패는 모두 `fail_stage`가 작업 사본을 되돌린다.
"""

import dataclasses
import os
import re
import stat
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from team_wiki_engine.claude.types import RunClaudeInput
from team_wiki_engine.git.ops import (
    changed_paths,
    commit_all,
    commit_paths,
    diff_names,
    reset_branch_to,
)
from team_wiki_engine.metrics.oq import question_headings
from team_wiki_engine.ops.notify import redact_secrets
from team_wiki_engine.sync.claude_step import WriteStep, run_write_step
from team_wiki_engine.sync.lint_loop import REAPPLY_COMMIT_MESSAGE, LintLoopResult, lint_loop
from team_wiki_engine.sync.post_check import has_unsafe_char
from team_wiki_engine.sync.pr import (
    Analysis,
    PrBodyInput,
    build_pr_body,
    deliver,
    extract_section,
    neutralize_mentions,
)
from team_wiki_engine.sync.prompts import cut_escaped, ingest_prompt, prompt_name, recheck_prompt
from team_wiki_engine.sync.script_fields import apply_baseline, script_value_paths
from team_wiki_engine.sync.stage import (
    DIRECT_LINT_MESSAGE,
    RUN_ID_PATTERN,
    LockNotHeldError,
    Mask,
    StageContext,
    StageError,
    append_log_locked,
    check_script_targets,
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
from team_wiki_engine.util.fs import read_text_if_exists
from team_wiki_engine.util.time import kst_iso, kst_parts
from team_wiki_engine.wiki.baseline import Baseline, RepoBaseline, write_baseline
from team_wiki_engine.wiki.lint_secrets import clean, safe_text, secret_findings
from team_wiki_engine.wiki.log import LogEntry

RAW_NAME: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}-.+\.md")
"""`fullmatch`로만 쓴다(`$`는 끝의 줄바꿈 앞에서도 맞는다). 숫자는 ASCII다."""
MAX_RAW_NAME_CHARS: Final = 200
MAX_RAW_PER_RUN: Final = 20
"""한 실행에서 반영하는 원본의 최대 개수(Claude 비용 상한). 나머지는 다음 실행에서 한다."""
MAX_RAW_BYTES: Final = 1024 * 1024
"""원본 한 파일의 최대 크기. 넘으면 Claude에게 보내지 않고 건너뛴다(비용·읽기 상한)."""
MAX_LOG_TARGET_CHARS: Final = 500
"""`wiki/log.md` 한 줄에 싣는 대상 목록의 최대 길이."""
MAX_INVALID_WARNINGS: Final = 20
"""형식 오류 원본마다 경고를 남기되, 이 개수를 넘으면 나머지는 한 줄로 센다(경고 범람 방지)."""

_DISPLAY_CHARS: Final = 80
_NO_LIMIT: Final = 1 << 30
_SKIPPED_PATH_CHARS: Final = 200
_MAX_SHOWN_SKIPPED: Final = 5
_ANALYSIS_HEADING: Final = "## 문서 분석"
_ANALYSIS_TAIL_CHARS: Final = 3000
_RECHECK_PATHSPECS: Final = ["wiki/decisions", "wiki/contracts"]

NOT_A_FILE: Final = "일반 파일이 아니라 건너뜀"
HAS_CONTROL: Final = "파일명에 제어 문자가 있어 건너뜀"
TOO_LONG: Final = "파일명이 너무 길어 건너뜀"
NOT_NFC: Final = "파일명이 NFC 형식이 아니라 건너뜀 — 이름을 다시 저장해 주세요"
SECRET_LIKE: Final = "파일명이 비밀값처럼 보여 건너뜀 — 이름을 바꿔 주세요"
BAD_FORMAT: Final = "파일명이 YYYY-MM-DD-로 시작하지 않아 건너뜀"
TOO_BIG: Final = "파일이 너무 커 건너뜀"
EMPTY_SUMMARY: Final = "문서: 변경 없음 (Claude 응답 없음)"
_EMPTY_STOP: Final = "Claude 응답이 비어 있고 바뀐 것이 없어 여기서 멈춤"


# ---------------------------------------------------------------- 원본 고르기 (G14)


@dataclass(frozen=True)
class RawScan:
    pending: list[str]
    """반영할 원본(NFC 이름, 이름순)."""
    invalid: list[tuple[str, str]]
    """건너뛴 원본: `(표시용 이름, 사유)`. 표시용 이름은 `clean` 뒤 80자 이하다."""


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _display(name: str, redact: Sequence[str] = ()) -> str:
    """표시용 이름: **전체를** 가리고(알려진 비밀값 → 비밀값 같은 단어 → 제어 문자) 80자로 자른다.

    자른 뒤에 가리면 경계에 걸친 비밀값 조각이 남으므로 순서가 중요하다.
    """
    return cut_escaped(safe_text(redact_secrets(name, redact), _NO_LIMIT), _DISPLAY_CHARS)


def _problem(name: str, regular: bool, redact: Sequence[str] = ()) -> str | None:
    """원본 이름을 못 쓰는 사유(없으면 `None`). 사유는 이 순서로 정한다."""
    if not regular:
        return NOT_A_FILE
    if has_unsafe_char(name):
        return HAS_CONTROL
    if len(name) > MAX_RAW_NAME_CHARS:
        return TOO_LONG
    if _nfc(name) != name:
        return NOT_NFC
    # 이름은 로그·페이지·PR에 오른다: 비밀값처럼 보이면(L1이 지적할 모양) 로그가 다음 실행의 점검을
    # 영구히 막는다. 서버가 아는 비밀값이 든 이름도 같다.
    if secret_findings(name) or redact_secrets(name, redact) != name:
        return SECRET_LIKE
    if RAW_NAME.fullmatch(name) is None:
        return BAD_FORMAT
    return None


def classify_raw_names(
    entries: Iterable[tuple[str, bool]], processed: Iterable[str], *, redact: Sequence[str] = ()
) -> RawScan:
    """`raw/` 최상위 항목 `(디스크 이름, 일반 파일인가)`를 반영할 것과 건너뛸 것으로 나눈다.

    `.md`로 끝나지 않는 것은 보지 않는다. 이미 처리한 이름(NFC로 비교)은 어디에도 넣지 않는다.
    `redact`는 서버가 아는 비밀값이다(이름에 들어 있으면 건너뛰고, 표시에서는 가린다).
    """
    done = {_nfc(name) for name in processed}
    pending: list[str] = []
    invalid: list[tuple[str, str]] = []
    for name, regular in sorted(entries):
        if not name.endswith(".md") or _nfc(name) in done:
            continue
        reason = _problem(name, regular, redact)
        if reason is None:
            pending.append(name)
        else:
            invalid.append((_display(name, redact), reason))
    return RawScan(pending=pending, invalid=invalid)


def _is_regular(entry: os.DirEntry[str]) -> bool:
    try:
        return entry.is_file(follow_symlinks=False)
    except OSError:
        return False


def _size(entry: os.DirEntry[str]) -> int:
    try:
        return entry.stat(follow_symlinks=False).st_size
    except OSError:
        return MAX_RAW_BYTES + 1  # 크기를 알 수 없으면 쓰지 않는다


def pending_raw_files(
    data_dir: Path, processed: Sequence[str], *, redact: Sequence[str] = ()
) -> RawScan:
    """`<data_dir>/raw/`의 **최상위** 항목만 본다(`raw/assets/` 같은 하위 폴더 아래는 안 본다).

    `raw/`가 없으면 빈 결과다. 링크이거나 폴더가 아니면 `NotADirectoryError`다. 1MiB를 넘는 파일은
    반영하지 않고 건너뛴 쪽에 `파일이 너무 커 건너뜀`으로 넣는다.
    """
    raw = os.path.join(os.fspath(data_dir), "raw")
    try:
        mode = os.lstat(raw).st_mode
    except FileNotFoundError:
        return RawScan(pending=[], invalid=[])
    if not stat.S_ISDIR(mode):
        raise NotADirectoryError("raw가 폴더가 아닙니다")
    entries: list[tuple[str, bool]] = []
    too_big: set[str] = set()
    with os.scandir(raw) as scan:
        for entry in scan:
            regular = _is_regular(entry)
            entries.append((entry.name, regular))
            if regular and _size(entry) > MAX_RAW_BYTES:
                too_big.add(entry.name)
    classified = classify_raw_names(entries, processed, redact=redact)
    oversized = [name for name in classified.pending if name in too_big]
    return RawScan(
        pending=[name for name in classified.pending if name not in too_big],
        invalid=sorted([*classified.invalid, *((_display(n, redact), TOO_BIG) for n in oversized)]),
    )


def _revalidate(data_dir: Path, name: str, redact: Sequence[str]) -> str | None:
    """프롬프트를 만들기 직전에 이름·파일 종류·크기를 다시 본다(목록을 만든 뒤 바뀔 수 있다)."""
    problem = _problem(name, True, redact)
    if problem is not None:
        return problem
    try:
        info = os.lstat(os.path.join(os.fspath(data_dir), "raw", name))
    except OSError:
        return NOT_A_FILE
    if not stat.S_ISREG(info.st_mode):
        return NOT_A_FILE
    return TOO_BIG if info.st_size > MAX_RAW_BYTES else None


def recheck_targets(paths: Sequence[str]) -> tuple[list[str], list[str]]:
    """재확인 대상과 건너뛴 경로(표시용). NFC가 아니거나 제어 문자가 든 경로는 건너뛴다.

    가드가 그런 경로를 거부하고 리눅스에서는 NFC 경로로 열 수 없어서 Claude에게 줘도 소용이 없다.
    """
    targets: list[str] = []
    skipped: list[str] = []
    for path in paths:
        if has_unsafe_char(path) or _nfc(path) != path:
            skipped.append(cut_escaped(clean(path), _SKIPPED_PATH_CHARS))
        else:
            targets.append(path)
    return targets, skipped


# ---------------------------------------------------------------- 단계


class _LockLostError(Exception):
    """단계 도중 `sync.lock`을 잃었다. 작업 사본이 더는 우리 것이 아니므로 건드리지 않고 끝낸다."""


async def run_ingest(ctx: StageContext) -> StageResult:
    stage = new_stage("ingest")
    if not ctx.lock.held:  # G11, S5: 잠금 없이는 Claude도 git도 부르지 않는다
        return fail_without_lock(stage)
    try:
        return await _run(ctx, stage)
    except _LockLostError:
        return fail_without_lock(stage)
    except StageError as exc:
        return await fail_stage(stage, ctx.deps.data_git, str(exc))
    except LockNotHeldError as exc:  # 고정 문구라 안전하다
        return await fail_stage(stage, ctx.deps.data_git, str(exc))


@dataclass
class _Progress:
    """원본을 반영해 가면서 쌓이는 상태."""

    baseline: Baseline
    """메모리의 현재 기준. 원본 하나를 기록할 때마다 새 객체로 바뀐다."""
    processed: list[str]
    analyses: list[Analysis]
    session_id: str | None = None
    stopped: Literal["post_check", "empty"] | None = None
    skipped: int = 0
    """목록을 만든 뒤 다시 확인하다 걸러진 원본 수."""


def _require_lock(ctx: StageContext) -> None:
    if not ctx.lock.held:
        raise _LockLostError


async def _step(
    ctx: StageContext, stage: StageResult, mask: Mask, inp: RunClaudeInput, prompt: str
) -> WriteStep:
    """Claude 쓰기 실행 한 번(쓰기 루트 사전 검사 → 실행 → 정리 대기 → 사후 검사) + 사용량 합산."""
    deps = ctx.deps
    # `run_write_step`이 던지면(사후 검사의 git 오류 등) 실행 결과를 받지 못해 사용량을 더할 수
    # 없다. 그 경우에도 단계는 가린 메시지로 실패한다(작업 사본은 `fail_stage`가 되돌린다).
    with guard(mask, "Claude 실행을 검사할 수 없습니다"):
        step = await run_write_step(
            runner=deps.runner,
            inp=dataclasses.replace(inp, prompt=prompt),
            git=deps.data_git,
            data_dir=deps.paths.data_sync_dir,
            drain=deps.drain,
            settle_timeout_s=deps.settle_timeout_s,
            redact=deps.redact,
        )
    if step.run is not None:
        add_usage(stage, step.run)
    return step


async def _run(ctx: StageContext, stage: StageResult) -> StageResult:
    deps = ctx.deps
    config, paths, git = deps.config, deps.paths, deps.data_git
    mask = make_mask(deps)
    base = config.data_repo.branch
    data_dir = paths.data_sync_dir
    if RUN_ID_PATTERN.fullmatch(ctx.run_id) is None:
        raise StageError("run_id 형식이 올바르지 않습니다")
    if config.review_mode == "pr" and deps.pr_port is None:  # Claude 비용을 쓰기 전에 막는다
        raise StageError("검토 방식이 pr인데 PR 포트가 없습니다")
    branch = work_branch("ingest", ctx.run_id)

    # 1. 같은 종류의 열린 PR이면 건너뛴다.
    if config.review_mode == "pr" and deps.pr_port is not None:
        with guard(mask, "열린 PR을 확인할 수 없습니다"):
            open_id = await deps.pr_port.find_open("ingest")
        if open_id is not None:
            stage.result = "skipped-open-pr"
            stage.summary = f"문서: 검토 대기 중({mask(open_id)})"
            return stage

    # 2. 작업 브랜치를 origin/<base>에서 새로 만든다.
    with guard(mask, "작업 브랜치를 만들 수 없습니다"):
        await run_to_completion(reset_branch_to(git, branch, f"origin/{base}"))

    # 3. 기준 읽기(G8).
    baseline = load_baseline(data_dir, deps.redact)

    # 4. 새 원본 고르기(G14).
    with guard(mask, "원본 목록을 읽을 수 없습니다"):
        check_script_targets(data_dir, dirs=("raw",))
        scan = pending_raw_files(data_dir, baseline.raw_processed, redact=deps.redact)
    shown = scan.invalid[:MAX_INVALID_WARNINGS]
    stage.warnings.extend(f"raw/{mask(name)}: {reason}" for name, reason in shown)
    if len(scan.invalid) > len(shown):
        stage.warnings.append(f"형식 오류 {len(scan.invalid) - len(shown)}건은 더 있어 경고를 생략")
    if not scan.pending:
        stage.summary = f"문서: 형식 오류 {len(scan.invalid)}건" if scan.invalid else "문서: 없음"
        return stage
    todo = scan.pending[:MAX_RAW_PER_RUN]
    if len(scan.pending) > len(todo):
        stage.warnings.append(f"남은 원본 {len(scan.pending) - len(todo)}건은 다음 실행에서 반영")

    questions_before = question_headings(
        read_text_if_exists(data_dir / "wiki" / "open-questions.md")
    )
    inp = RunClaudeInput(
        profile="ingest",
        prompt="",
        cwd=data_dir,
        read_roots=(paths.repos_dir,),
        write_roots=(data_dir / "wiki",),
        timeout_ms=config.limits.sync_timeout_ms,
    )
    progress = _Progress(baseline=baseline, processed=[], analyses=[])

    # 5. 원본마다: Claude → 사후 검사 → 커밋 → 처리 기록 → 커밋.
    failed = await _ingest_files(ctx, stage, mask, inp, todo, progress)
    if failed is not None:
        return failed
    if not progress.processed:
        if progress.stopped == "post_check":  # 마지막 경고가 멈춘 이유다(TS)
            return await fail_stage(stage, git, stage.warnings[-1])
        if progress.stopped == "empty":
            stage.summary = EMPTY_SUMMARY
        else:  # 목록을 만든 뒤 모두 걸러졌다
            stage.summary = f"문서: 형식 오류 {len(scan.invalid) + progress.skipped}건"
        return stage

    # 6. 바뀐 결정·약속이 있으면 재확인을 한 번 한다.
    failed = await _recheck(ctx, stage, mask, inp, progress)
    if failed is not None:
        return failed

    # 7. 점검 루프 → 로그 → PR 본문·전달.
    loop = await _lint(ctx, stage, mask, inp, progress)
    if isinstance(loop, StageResult):
        return loop
    _require_lock(ctx)  # 로그를 쓰기 전에 다시 본다
    pages = await _write_log(ctx, stage, mask, progress)
    return await _deliver(ctx, stage, mask, progress, loop, pages, questions_before, branch)


async def _ingest_files(
    ctx: StageContext,
    stage: StageResult,
    mask: Mask,
    inp: RunClaudeInput,
    names: Sequence[str],
    progress: _Progress,
) -> StageResult | None:
    """원본을 하나씩 반영한다. 단계를 끝낼 이유(실패)가 있으면 그 결과, 아니면 `None`."""
    deps = ctx.deps
    git, data_dir = deps.data_git, deps.paths.data_sync_dir
    today = kst_parts(ctx.now).date
    for name in names:
        _require_lock(ctx)
        problem = _revalidate(data_dir, name, deps.redact)  # TOCTOU: 방금 확인한 이름만 싣는다
        if problem is not None:
            # 목록을 만든 뒤 `raw/`가 바뀌었다면 작업 사본이 더럽다 — 그대로 두면 뒤 커밋에 섞인다.
            with guard(mask, "변경 여부를 확인할 수 없습니다"):
                dirty = await run_to_completion(changed_paths(git))
            if dirty:
                return await fail_stage(stage, git, "원본 목록을 만든 뒤 raw/가 바뀌어 중단합니다")
            stage.warnings.append(f"raw/{mask(_display(name, deps.redact))}: {problem}")
            progress.skipped += 1
            continue
        label = mask(prompt_name(name))
        step = await _step(ctx, stage, mask, inp, ingest_prompt(name, progress.processed, today))
        _require_lock(ctx)  # Claude는 오래 걸린다 — 커밋 전에 잠금을 다시 본다
        if step.status in ("unsafe_write_root", "unsettled"):
            return await fail_stage(stage, git, mask(step.message))
        if step.status == "claude_failed":
            return await fail_stage(stage, git, f"{label} 반영 중 {mask(step.message)}")
        if step.status == "post_check_failed":  # 사후 검사가 이미 되돌렸다
            offending = mask(", ".join(step.offending))
            stage.warnings.append(f"{label}: 허용 경로 밖 변경({offending})이 있어 여기서 멈춤")
            progress.stopped = "post_check"
            break
        result = step.run
        if result is None:  # `ok`면 항상 있다
            return await fail_stage(stage, git, "Claude 실행 결과가 없습니다")
        if result.failure == "empty":  # 스펙 §7.9, G21
            with guard(mask, "변경 여부를 확인할 수 없습니다"):
                changed = await run_to_completion(changed_paths(git))
            if not changed:
                stage.warnings.append(f"{label}: {_EMPTY_STOP}")  # 처리됨으로 적지 않는다
                progress.stopped = "empty"
                break
        with guard(mask, "커밋할 수 없습니다"):
            await run_to_completion(commit_all(git, f"wiki: {neutralize_mentions(name)} 반영"))
        await _record_processed(ctx, mask, progress, name)  # 처리 기록도 별도 커밋(결정 ④)
        analysis = extract_section(result.text, _ANALYSIS_HEADING)
        if analysis is None:
            analysis = result.text[-_ANALYSIS_TAIL_CHARS:]
        # Claude가 읽은 서버 경로·알려진 비밀값이 PR 본문에 새지 않게 한다.
        analysis = scrub_paths(
            redact_secrets(analysis, deps.redact), path_roots(deps.paths), generic=False
        )
        progress.analyses.append(Analysis(title=name, text=analysis))
        progress.processed.append(name)
        progress.session_id = result.session_id or progress.session_id
    return None


def _next_baseline(current: Baseline, name: str, ctx: StageContext) -> Baseline:
    """`name`을 처리한 것으로 적은 다음 기준. 기존 객체는 바꾸지 않는다."""
    return Baseline(
        repos={n: RepoBaseline(r.commit, r.archived) for n, r in current.repos.items()},
        raw_processed=[*current.raw_processed, name],
        updated_at=kst_iso(ctx.now),
    )


async def _record_processed(ctx: StageContext, mask: Mask, progress: _Progress, name: str) -> None:
    """`baseline.json`에 `name`을 처리한 것으로 적고 따로 커밋한다. 메모리의 기준도 그때 앞선다."""
    deps = ctx.deps
    data_dir = deps.paths.data_sync_dir
    following = _next_baseline(progress.baseline, name, ctx)
    with guard(mask, "처리 기록을 쓸 수 없습니다"):
        check_script_targets(data_dir, dirs=("wiki",), files=("baseline.json",))
        write_baseline(data_dir, following)
        await run_to_completion(
            commit_paths(
                deps.data_git, ["baseline.json"], f"chore: {neutralize_mentions(name)} 처리 기록"
            )
        )
    progress.baseline = following


async def _recheck(
    ctx: StageContext, stage: StageResult, mask: Mask, inp: RunClaudeInput, progress: _Progress
) -> StageResult | None:
    """바뀐 결정·약속에 기댄 페이지를 Claude가 한 번 다시 보게 한다. 실패해도 단계는 이어 간다."""
    deps = ctx.deps
    git = deps.data_git
    with guard(mask, "바뀐 결정·약속을 확인할 수 없습니다"):
        changed = await diff_names(
            git, f"origin/{deps.config.data_repo.branch}", "HEAD", _RECHECK_PATHSPECS
        )
    targets, skipped = recheck_targets(changed)
    if skipped:
        listing = ", ".join(mask(path) for path in skipped[:_MAX_SHOWN_SKIPPED])
        more = " 외" if len(skipped) > _MAX_SHOWN_SKIPPED else ""
        stage.warnings.append(
            f"재확인에서 뺀 경로 {len(skipped)}건(NFC가 아니거나 제어 문자가 있음): {listing}{more}"
        )
    if not targets:
        return None
    _require_lock(ctx)
    prompt = recheck_prompt(targets, kst_parts(ctx.now).date)
    step = await _step(ctx, stage, mask, inp, prompt)
    _require_lock(ctx)
    if step.status in ("unsafe_write_root", "unsettled"):
        return await fail_stage(stage, git, mask(step.message))
    if step.status == "post_check_failed":  # 사후 검사가 이미 되돌렸다
        stage.warnings.append(
            f"재확인: 허용 경로 밖 변경({mask(', '.join(step.offending))})을 버림"
        )
        return None
    if step.status == "claude_failed":
        stage.warnings.append(f"재확인 실패({mask(step.message)}) — 재확인 없이 진행")
        return None
    with guard(mask, "커밋할 수 없습니다"):
        await run_to_completion(commit_all(git, f"wiki: 재확인 수정 {ctx.run_id}"))
    if step.run is not None:
        progress.session_id = step.run.session_id or progress.session_id
    return None


async def _lint(
    ctx: StageContext, stage: StageResult, mask: Mask, inp: RunClaudeInput, progress: _Progress
) -> LintLoopResult | StageResult:
    """점검 → 같은 세션으로 한 번 수정(G9: 점검을 할 수 없으면 단계 실패)."""
    _require_lock(ctx)  # 점검 수정은 Claude를 다시 부른다
    deps = ctx.deps
    git, data_dir = deps.data_git, deps.paths.data_sync_dir
    final = (
        progress.baseline
    )  # 메모리의 기준을 쓴다(`read_baseline`을 다시 부르면 예외가 날 수 있다)

    def reapply() -> None:
        check_script_targets(data_dir, dirs=("wiki", "wiki/status"), files=("baseline.json",))
        apply_baseline(data_dir, final)

    # 기준 값은 스크립트가 쓴다(스펙 D10·D16): Claude(원본 반영·재확인)가 `wiki/status/*.md`의
    # `baseline_commit`·`archived`를 고쳤더라도 점검 전에 스크립트 값으로 되돌린다.
    # 앞에 Claude 실행이 없었을 수도 있다(재확인 대상 없음, 뒤 원본에서 멈춤): 사본이 깨끗한지
    # 먼저 본다. 그리고 스크립트가 쓰는 파일만 커밋한다(`commit_all`로 쓸어 담지 않는다).
    await ensure_clean_tree(git, mask)
    with guard(mask, "기준 값을 쓸 수 없습니다"):
        reapply()
        paths = script_value_paths(data_dir)
        if paths:
            await run_to_completion(commit_paths(git, paths, REAPPLY_COMMIT_MESSAGE))
    with guard(mask, "점검 중 오류가 났습니다"):
        loop = await lint_loop(
            data_dir=data_dir,
            git=git,
            runner=deps.runner,
            inp=inp,
            session_id=progress.session_id,
            reapply=reapply,
            fix_commit_message=f"wiki: 점검 위반 수정 {ctx.run_id}",
            today=kst_parts(ctx.now).date,
            drain=deps.drain,
            settle_timeout_s=deps.settle_timeout_s,
            redact=deps.redact,
        )
    for run in loop.runs:
        add_usage(stage, run)
    if loop.failure is not None:
        return await fail_stage(stage, git, mask(loop.failure))
    stage.lint_failed = bool(loop.violations)
    return loop


async def _write_log(
    ctx: StageContext, stage: StageResult, mask: Mask, progress: _Progress
) -> list[str]:
    """반영 이력(사후 검사 뒤, 잠금을 쥔 채로). 바뀐 페이지 목록을 돌려준다."""
    deps = ctx.deps
    git, data_dir = deps.data_git, deps.paths.data_sync_dir
    with guard(mask, "반영 이력을 쓸 수 없습니다"):
        pages = await wiki_pages_changed(git, deps.config.data_repo.branch)
        check_script_targets(data_dir, dirs=("wiki",), files=("wiki/log.md",))
        entry = LogEntry(
            at=ctx.now,
            kind="ingest",
            targets=cut_escaped(", ".join(progress.processed), MAX_LOG_TARGET_CHARS),
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
    mask: Mask,
    progress: _Progress,
    loop: LintLoopResult,
    pages: list[str],
    questions_before: list[str],
    branch: str,
) -> StageResult:
    """PR 본문·전달. 전달이 끝나야 작업 브랜치가 `origin`에 가고 기준이 앞선다."""
    _require_lock(ctx)  # 잠금 없이는 push도 PR도 하지 않는다
    deps = ctx.deps
    config, paths = deps.config, deps.paths
    questions_after = question_headings(
        read_text_if_exists(paths.data_sync_dir / "wiki" / "open-questions.md")
    )
    port_warnings = pr_port_warnings(deps.pr_port)
    body = build_pr_body(
        PrBodyInput(
            reflected=[f"raw/{name}" for name in progress.processed],
            changed_pages=[f"wiki/{page}" for page in pages],
            new_questions=difference(questions_after, questions_before),
            resolved_questions=difference(questions_before, questions_after),
            analyses=progress.analyses,
            low_confidence=None,  # 확신이 낮은 부분은 문서별 분석 안에 있다
            lint_violations=loop.violations,
            warnings=[*stage.warnings, *port_warnings],
            run_id=ctx.run_id,
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
            kind="ingest",
            run_id=ctx.run_id,
            branch=branch,
            base=config.data_repo.branch,
            title=f"위키 문서 반영 {ctx.run_id}",
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
    target = delivered.pr_id or "main 직접 반영"
    suffix = f" (점검 실패 {len(loop.violations)}건)" if stage.lint_failed else ""
    stage.summary = f"문서: {len(progress.processed)}건 → {target}{suffix}"
    return stage
