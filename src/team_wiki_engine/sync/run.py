"""동기화 한 번의 실행: 잠금 → 데이터 사본 → 측정 → 코드 반영 → 정리 대기 → 문서 반영 → 보고.

규칙:
- **잠금은 항상 놓는다.** `acquire_lock`이 `SyncLock`을 돌려준 뒤의 모든 일은 `try/finally`
  안에서 한다(`CancelledError` 포함). 잠금을 못 잡았을 때만 `read_lock_owner`를 부른다(그때만
  내용이 지금 쥔 쪽의 것이다).
- **취소**: `CancelledError`는 잠금을 놓은 뒤 그대로 전파한다. 단계 기록(`sync-runs.jsonl`)은
  **단계가 끝나는 즉시** 한 줄씩 쓴다 — 뒤 단계가 실패하거나 취소돼도 끝난 단계의 기록은 남고,
  취소로 끊긴 단계와 보고(운영 채널)는 남지 않는다. 이 모듈은 git 변경을 직접 하지 않는다
  (되돌리기는 각 단계가 `run_to_completion`으로 한다).
- **정리 대기(G1, 이월 1)**: 코드 단계 뒤 문서 단계 전에 `deps.drain`을 정확히 한 번 부른다.
  남았거나(`> 0`) 대기 자체가 예외를 던지면(정리 여부를 모른다) 문서 단계를 돌리지 않고 `failed`.
- **가림**: 운영 채널·보고·경고로 가는 모든 글은 `safe_message`(비밀값 → 서버 경로 → 위험 단어·
  자르기)나 `Mask`를 지난다. 데이터 사본 준비 실패는 가린 메시지의 `SyncRunError`로 올린다
  (원본 예외를 연쇄하지 않는다) — `main`이 그 메시지를 보고한다.
- 측정(미결 추적·PR 결과 수집)과 기록 쓰기·보고 전송의 실패는 **경고**이지 중단이 아니다.
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from team_wiki_engine.metrics.oq import track_open_questions
from team_wiki_engine.metrics.pr_review import collect_pr_reviews
from team_wiki_engine.metrics.record import (
    STATE_FILES,
    OqEventRecord,
    PrReviewRecord,
    SyncRunRecord,
    append_jsonl,
)
from team_wiki_engine.ops.notify import redact_secrets
from team_wiki_engine.sync.code_sync import run_code_sync
from team_wiki_engine.sync.data_copy import prepare_data_copy
from team_wiki_engine.sync.ingest import run_ingest
from team_wiki_engine.sync.lock import SyncLock, acquire_lock, held_too_long, read_lock_owner
from team_wiki_engine.sync.stage import (
    KIND_LABEL,
    Mask,
    StageContext,
    SyncDeps,
    describe_error,
    make_mask,
    safe_message,
    scrub_paths,
)
from team_wiki_engine.sync.types import StageResult, SyncKind, new_stage
from team_wiki_engine.util.time import kst_iso, kst_parts

BUSY_MESSAGE: Final = "[sync] 이미 실행 중이라 건너뜀"
LONG_HELD_SUFFIX: Final = "잠금이 6시간 넘게 유지됨"
UNSETTLED_SKIP_MESSAGE: Final = "이전 Claude 실행이 정리되지 않아 문서 반영을 건너뜀"
_KINDS: Final = (None, "code", "ingest")


class SyncRunError(Exception):
    """실행 준비 단계(데이터 사본 준비)의 실패. 메시지는 운영 채널에 그대로 써도 안전하다."""


@dataclass(frozen=True)
class SyncRunReport:
    run_id: str
    stages: tuple[StageResult, ...]
    pr_reviews: tuple[PrReviewRecord, ...]
    oq_events: tuple[OqEventRecord, ...]
    warnings: tuple[str, ...]
    """실행 전체의 경고(측정 실패·기록 쓰기 실패·보고 전송 실패). 단계 경고는 각 단계에 있다."""
    line: str
    """운영 채널에 보낸 한 줄(비밀값·서버 경로를 가린 뒤)."""


def format_report(run_id: str, stages: Sequence[StageResult]) -> str:
    cost = sum(stage.cost_usd for stage in stages)
    parts = [*(stage.summary for stage in stages), f"비용 ${cost:.2f}"]
    return f"[sync {run_id}] " + " · ".join(parts)


async def safe_stage(
    kind: SyncKind,
    run: Callable[[], Awaitable[StageResult]],
    redact: Sequence[str],
    *,
    roots: Mapping[str, str] | None = None,
) -> StageResult:
    """단계를 돌리고, `Exception`은 가린 메시지의 `failed` 단계로 바꾼다. 취소는 전파한다."""
    try:
        return await run()
    except Exception as exc:
        try:
            text = str(exc)
        except Exception:  # noqa: BLE001 - 문자열로 못 바꾸는 예외도 단계 실패로 끝낸다
            text = ""
        error = safe_message(text or type(exc).__name__, redact, roots=roots)
        stage = new_stage(kind)
        stage.result = "failed"
        stage.error = error
        stage.summary = f"{KIND_LABEL[kind]}: 실패 — {error}"
        return stage


def _describe(exc: Exception, mask: Mask) -> str:
    return describe_error(exc, mask) or type(exc).__name__


def _publish(text: str, mask: Mask) -> str:
    """이미 가린 글에 한 번 더: 알려진 비밀값과 서버 경로만 가린다(자르거나 바꾸지 않는다)."""
    return scrub_paths(redact_secrets(text, mask.redact), mask.roots)


async def _send(deps: SyncDeps, text: str, *, mention: bool, warnings: list[str] | None) -> None:
    """운영 채널로 보낸다. 알림 실패는 경고(`warnings`가 있을 때)로만 남긴다. 취소는 전파."""
    try:
        await deps.notifier.send(text, mention_admin=mention)
    except Exception as exc:
        if warnings is not None:
            warnings.append(f"보고 전송 실패: {_describe(exc, make_mask(deps))}")


async def run_once(deps: SyncDeps, *, only: SyncKind | None = None) -> SyncRunReport | None:
    """동기화를 한 번 돈다. 이미 누가 돌고 있으면 알리고 `None`.

    `only`가 `"code"`면 코드 단계만, `"ingest"`면 문서 단계만 돈다(둘 다 `None`이면 코드 → 정리
    대기 → 문서). 데이터 사본 준비 실패는 `SyncRunError`, 잠금 파일 오류는 `LockError`로 올라간다.
    """
    if only not in _KINDS:
        raise ValueError("only는 None, 'code', 'ingest' 중 하나여야 합니다")
    now = deps.now()
    run_id = kst_parts(now).run_id
    lock_file = deps.paths.state_dir / STATE_FILES.lock
    lock = acquire_lock(lock_file, now=now)
    if lock is None:
        owner = read_lock_owner(lock_file)  # 잠금을 못 잡은 직후에만 의미가 있다
        if held_too_long(owner, now):
            pid = "알 수 없음" if owner.pid is None else str(owner.pid)
            text = f"{BUSY_MESSAGE} — {LONG_HELD_SUFFIX}(pid {pid})"
            await _send(deps, text, mention=True, warnings=None)  # G18: 알리되 가져오지는 않는다
        else:
            await _send(deps, BUSY_MESSAGE, mention=False, warnings=None)
        return None
    try:
        return await _run_locked(deps, lock, only, now, run_id)
    finally:
        lock.release()


async def _run_locked(
    deps: SyncDeps, lock: SyncLock, only: SyncKind | None, now: datetime, run_id: str
) -> SyncRunReport:
    mask = make_mask(deps)
    warnings: list[str] = []

    try:
        await prepare_data_copy(
            deps.data_git, deps.config.data_repo.url, deps.config.data_repo.branch
        )
    except Exception as exc:
        raise SyncRunError(f"데이터 사본 준비 실패: {_describe(exc, mask)}") from None

    oq_events: tuple[OqEventRecord, ...] = ()
    pr_reviews: tuple[PrReviewRecord, ...] = ()
    try:
        oq_events = tuple(track_open_questions(deps.paths.data_sync_dir, deps.paths.state_dir, now))
    except Exception as exc:
        warnings.append(f"측정 실패: 미결 추적 — {_describe(exc, mask)}")
    if deps.pr_port is not None:
        try:
            pr_reviews = tuple(
                await collect_pr_reviews(
                    state_dir=deps.paths.state_dir,
                    pr_port=deps.pr_port,
                    git=deps.data_git,
                    now=now,
                )
            )
        except Exception as exc:
            warnings.append(f"측정 실패: PR 결과 수집 — {_describe(exc, mask)}")

    stages: list[StageResult] = []

    def record(stage: StageResult) -> None:
        stages.append(stage)
        try:
            append_jsonl(
                deps.paths.state_dir / STATE_FILES.sync_runs,
                SyncRunRecord(
                    at=kst_iso(now),
                    run_id=run_id,
                    kind=stage.kind,
                    result=stage.result,
                    pr=stage.pr_id,
                    lint_failed=stage.lint_failed,
                    cost_usd=stage.cost_usd,
                    input_tokens=stage.input_tokens,
                    output_tokens=stage.output_tokens,
                    duration_ms=stage.duration_ms,
                ),
            )
        except (OSError, ValueError) as exc:  # pydantic의 ValidationError는 ValueError다
            warnings.append(f"기록 실패: {stage.kind} — {_describe(exc, mask)}")

    ctx = StageContext(deps=deps, run_id=run_id, now=now, lock=lock)

    if only != "ingest":
        record(await safe_stage("code", lambda: run_code_sync(ctx), deps.redact, roots=mask.roots))

    if only is None:
        # 코드 단계의 Claude가 시간 초과로 버려졌어도 CLI 정리가 끝나기 전에 문서 단계가 같은
        # 작업 사본에 쓰면 안 된다(G1). 경계마다 정확히 한 번 기다린다.
        try:
            pending = await deps.drain(deps.settle_timeout_s)
        except Exception:  # noqa: BLE001 - 정리 여부를 모르면 정리되지 않은 것으로 본다
            pending = 1
        if pending > 0:
            skipped = new_stage("ingest")
            skipped.result = "failed"
            skipped.error = UNSETTLED_SKIP_MESSAGE
            skipped.summary = f"{KIND_LABEL['ingest']}: 실패 — {UNSETTLED_SKIP_MESSAGE}"
            record(skipped)
            return await _finish(deps, run_id, stages, pr_reviews, oq_events, warnings, mask)

    if only != "code":
        record(await safe_stage("ingest", lambda: run_ingest(ctx), deps.redact, roots=mask.roots))
    return await _finish(deps, run_id, stages, pr_reviews, oq_events, warnings, mask)


async def _finish(
    deps: SyncDeps,
    run_id: str,
    stages: list[StageResult],
    pr_reviews: tuple[PrReviewRecord, ...],
    oq_events: tuple[OqEventRecord, ...],
    warnings: list[str],
    mask: Mask,
) -> SyncRunReport:
    text = format_report(run_id, stages)
    if warnings:
        text += f" · 경고 {len(warnings)}건"
    line = _publish(text, mask)
    failed = any(stage.result == "failed" for stage in stages)
    await _send(deps, line, mention=failed, warnings=warnings)
    return SyncRunReport(
        run_id=run_id,
        stages=tuple(stages),
        pr_reviews=pr_reviews,
        oq_events=oq_events,
        warnings=tuple(warnings),
        line=line,
    )
