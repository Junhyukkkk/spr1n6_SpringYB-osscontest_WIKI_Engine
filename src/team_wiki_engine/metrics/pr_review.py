"""M1: 봇 PR이 닫힌 뒤 사람이 `wiki/`를 얼마나 고쳤는지 기록한다."""

from datetime import datetime
from pathlib import Path
from typing import Final

from team_wiki_engine.git.client import GitClient
from team_wiki_engine.git.ops import is_sha, numstat
from team_wiki_engine.metrics.record import STATE_FILES, PrReviewRecord, append_jsonl, read_jsonl
from team_wiki_engine.sync.pr_port import (
    PrOutcome,
    PrPort,
    TrackedPr,
    read_tracking,
    write_tracking,
)
from team_wiki_engine.util.time import kst_iso, parse_instant

MAX_REVIEWS_PER_RUN: Final = 100
"""한 번에 결과를 묻는 PR 수 상한(조회마다 네트워크를 쓴다). 나머지는 다음 실행이 먼저 본다."""


def _closed_at(value: object) -> str | None:
    parsed = parse_instant(value)
    return None if parsed is None else kst_iso(parsed)


async def collect_pr_reviews(
    *, state_dir: Path, pr_port: PrPort, git: GitClient, now: datetime
) -> list[PrReviewRecord]:
    """닫힌 PR을 `pr-review.jsonl`에 기록하고 추적에서 뺀다. 이번에 새로 만든 기록을 돌려준다.

    - **중단에 안전하다**: PR 하나를 기록할 때마다 줄어든 추적 목록을 바로 저장한 뒤 다음 PR로
      간다. 기록과 저장 사이에 죽어도 다시 돌리면 `(pr, kind)`가 이미 기록돼 있어 다시 쓰지 않고
      (그 PR은 결과도 다시 묻지 않는다) 추적에서만 뺀다.
    - **공정하다**: 추적 순서대로 한 번에 `MAX_REVIEWS_PER_RUN`개까지만 묻고, 묻고도 남은 PR(열림,
      조회 실패, 이해 못 할 결과, 기록 실패)은 목록 뒤로 보낸다. 안 물은 PR이 앞에 서므로 계속 열려
      있거나 실패하는 PR이 나머지를 굶기지 않는다.
    - 머지이고 `final_head_sha`가 SHA 모양일 때만 최종 커밋을 가져와 `bot_head_sha`와 비교한다
      (실패하면 측정 불가 `None`). `outcome`이 어떤 `Exception`을 던져도 그 PR은 추적에 남긴다.
      취소·중단(`BaseException`)은 삼키지 않는다. 기록을 쓰지 못한 PR도 추적에 남는다.
    """
    tracked = read_tracking(state_dir)
    review_file = state_dir / STATE_FILES.pr_review
    recorded = {(r.pr, r.kind) for r in read_jsonl(review_file, PrReviewRecord, tail=True)}
    stamp = kst_iso(now)
    records: list[PrReviewRecord] = []
    kept: list[TrackedPr] = []  # 물었지만 아직 추적해야 하는 PR(뒤로 보낸다)
    for index, pr in enumerate(tracked[:MAX_REVIEWS_PER_RUN]):
        unprocessed = tracked[index + 1 :]
        if (pr.id, pr.kind) in recorded:
            write_tracking(state_dir, [*unprocessed, *kept])  # 이미 기록됨: 추적에서만 뺀다
            continue
        try:
            outcome = await pr_port.outcome(pr)
        except Exception:
            kept.append(pr)
            continue
        if not isinstance(outcome, PrOutcome) or outcome.state not in ("merged", "rejected"):
            kept.append(pr)  # 열려 있거나 이해할 수 없는 결과
            continue
        files: int | None = None
        additions: int | None = None
        deletions: int | None = None
        if outcome.state == "merged" and is_sha(outcome.final_head_sha):
            final = outcome.final_head_sha
            try:
                await pr_port.fetch_final_head(pr)
                stats = await numstat(git, pr.bot_head_sha, str(final), "wiki/")
            except Exception:
                pass
            else:
                files, additions, deletions = stats.files, stats.additions, stats.deletions
        try:
            record = PrReviewRecord(
                at=stamp,
                pr=pr.id,
                kind=pr.kind,
                outcome=outcome.state,
                human_changed_files=files,
                human_additions=additions,
                human_deletions=deletions,
                closed_at=_closed_at(outcome.closed_at),
            )
            append_jsonl(review_file, record)
        except (OSError, ValueError):
            kept.append(pr)
            continue
        recorded.add((pr.id, pr.kind))
        records.append(record)
        write_tracking(state_dir, [*unprocessed, *kept])
    write_tracking(state_dir, [*tracked[MAX_REVIEWS_PER_RUN:], *kept])
    return records
