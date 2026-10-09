"""점검 → 같은 세션으로 한 번 수정(G5·G9).

Claude의 수정 실행은 `run_write_step`을 지나므로 사후 검사가 **스크립트의 `append_log`·기준 값
쓰기보다 먼저** 돈다. 수정은 한 번뿐이고, 다시 점검한 결과를 그대로 돌려준다.
"""

import dataclasses
import inspect
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from team_wiki_engine.claude.types import ClaudeRunner, RunClaudeInput, RunClaudeResult
from team_wiki_engine.git.client import GitClient
from team_wiki_engine.git.ops import changed_paths, commit_all, commit_paths
from team_wiki_engine.sync.claude_step import Drain, discard_quietly, run_write_step
from team_wiki_engine.sync.prompts import lint_fix_prompt
from team_wiki_engine.sync.script_fields import script_value_paths
from team_wiki_engine.util.aio import run_to_completion
from team_wiki_engine.wiki.lint import LintUsageError, LintViolation, lint
from team_wiki_engine.wiki.lint_secrets import safe_text

_USAGE_ERROR_CHARS = 200
REAPPLY_COMMIT_MESSAGE = "chore: 기준 값 다시 맞춤"
DIRTY_MESSAGE = "작업 사본에 커밋되지 않은 변경이 있어 중단합니다"


@dataclass(frozen=True)
class LintLoopResult:
    """`failure`가 있으면 단계 실패다(커밋하지 않고 끝낸다). 없으면 `violations`가 점검 결과다."""

    violations: tuple[LintViolation, ...]
    runs: tuple[RunClaudeResult, ...]
    failure: str | None


def _usage_failure(exc: LintUsageError) -> str:
    return "점검을 할 수 없습니다: " + safe_text(str(exc), _USAGE_ERROR_CHARS)


async def lint_loop(
    *,
    data_dir: Path,
    git: GitClient,
    runner: ClaudeRunner,
    inp: RunClaudeInput,
    session_id: str | None,
    reapply: Callable[[], object],
    fix_commit_message: str,
    today: str,
    drain: Drain,
    settle_timeout_s: float,
    redact: Sequence[str] = (),
) -> LintLoopResult:
    try:
        first = tuple(lint(data_dir))
    except LintUsageError as exc:
        return LintLoopResult((), (), _usage_failure(exc))
    if not first or session_id is None:
        return LintLoopResult(first, (), None)

    fix_inp = dataclasses.replace(
        inp, prompt=lint_fix_prompt(first, today), resume_session_id=session_id
    )
    step = await run_write_step(
        runner=runner,
        inp=fix_inp,
        git=git,
        data_dir=data_dir,
        drain=drain,
        settle_timeout_s=settle_timeout_s,
        redact=redact,
    )
    runs = (step.run,) if step.run is not None else ()
    if step.status == "claude_failed":
        return LintLoopResult(first, runs, None)  # TS: PR은 점검 실패로 나간다
    if step.status != "ok":  # unsafe_write_root, unsettled, post_check_failed
        return LintLoopResult(first, runs, step.message)

    async def commit_and_reapply() -> bool:
        """수정을 커밋하고 기준 값을 맞춘다. 사본이 더러우면 `False`(더 커밋하지 않는다)."""
        # 수정 실행은 `run_write_step`이 검사했으므로 `wiki/` 변경은 통째로 커밋한다.
        await commit_all(git, fix_commit_message)
        if await changed_paths(git):  # 그 밖의 변경은 스크립트 몫이 아니다 — 섞어 커밋하지 않는다
            return False
        applied = reapply()
        if inspect.isawaitable(applied):
            if inspect.iscoroutine(applied):
                applied.close()  # "never awaited" 경고를 막는다
            raise TypeError("reapply는 동기 함수여야 합니다")
        paths = script_value_paths(data_dir)
        if paths:  # 스크립트가 쓰는 기준 값 파일만 담는다
            await commit_paths(git, paths, REAPPLY_COMMIT_MESSAGE)
        return True

    try:
        # git 변경은 도중에 취소되면 index.lock을 남기므로 시작한 일은 끝까지 한 뒤 취소를 전파한다.
        clean = await run_to_completion(commit_and_reapply())
    except GeneratorExit:
        raise
    except BaseException:
        await discard_quietly(git)  # 커밋하지 못한 Claude·스크립트 변경을 남기지 않는다
        raise
    if not clean:
        await discard_quietly(git)
        return LintLoopResult(first, runs, DIRTY_MESSAGE)
    try:
        return LintLoopResult(tuple(lint(data_dir)), runs, None)
    except LintUsageError as exc:
        return LintLoopResult(first, runs, _usage_failure(exc))
