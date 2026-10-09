"""Claude 쓰기 실행 한 번의 관문(`run_write_step`).

동기화의 **모든** Claude 쓰기 실행이 이 함수를 지난다(계획 2 G1·G4·G5·G6). 순서는 고정이다.

0. 진입 검사(이 순서) — `inp.cwd`·`git.cwd`·`data_dir`이 같은 폴더인지(다르면 다른 폴더에
   `wiki/`를 만들지 않도록 가장 먼저), 쓰기 루트 사전 검사(`check_write_root`), 작업 사본에
   커밋되지 않은 변경(무시된 파일 포함)이 없는지(`status`는 `run_to_completion`으로 돌리고
   `--no-optional-locks`·`GIT_OPTIONAL_LOCKS=0`이라 취소돼도 락을 남기지 않는다).
   하나라도 걸리면 Claude를 부르지 않고 `drain`도 부르지 않으며 `unsafe_write_root`(`run=None`)
   다. 호출자는 쓰기 전에 커밋해 두어야 한다.
1. 실행(`runner`).
2. 정리 대기(`drain`) — 실행마다 **정확히 한 번**. 남은 게 있으면 되돌리고 `unsettled`.
3. 실패 처리 — `ok`가 아니고 `empty`도 아니면 되돌리고 `claude_failed`.
4. 사후 검사(`verify_claude_changes`) — 위반이면 (그 함수가 되돌리고) `post_check_failed`.
5. 모두 지나면 `ok`. 변경은 **커밋하지 않은 채** 둔다(커밋은 호출자 몫).

`ok`가 아닌 모든 경로는 작업 사본을 마지막 커밋으로 되돌린 상태로 끝난다. 되돌리기는 사후 검사와
같은 정리(`wiki/`의 링크 제거, 읽을 수 없는 폴더 열기 → `reset --hard` + `clean -ffdx`)를 한다.
이 함수는 커밋하지 않는다. 메시지에는 비밀값·제어 문자가 들어가지 않는다(G6): Claude의 `detail`은
`safe_text`로 가리고 자르며, 예외는 **종류 이름만** 싣는다(예외 문구에 비밀값이 있을 수 있다).

공개 도우미: `discard_quietly(git)` — 되돌리기를 취소돼도 끝까지 돌리고 실패는 삼키는 정리 함수.
`lint_loop`도 쓴다.

예외·취소(문서화한 규칙):
- 러너가 `Exception`을 던지면 실패 결과(`failure="error"`)로 바꿔 2~3단계를 그대로 거친다
  (drain도 한 번 부른다). 러너는 원래 예외를 던지지 않으므로 이는 가짜·버그 방어다.
- `drain`이 `Exception`을 던지면 정리 여부를 알 수 없으므로 `unsettled`로 본다.
- 사후 검사 등이 던진 예외는 되돌리기를 시도한 뒤(실패는 무시) 그대로 전파한다.
- 바깥 취소(`CancelledError`)와 그 밖의 `BaseException`(`KeyboardInterrupt` 등)은 어느 단계에서든
  되돌린 뒤 전파한다. 아직 drain을 부르지 않았으면(러너 실행 중) drain을 한 번 부르고,
  drain 도중이면 다시 부르지 않는다(실행마다 한 번). **되돌리기는 `run_to_completion`으로 돌려
  취소가 몇 번 오든(정리 drain 도중의 두 번째 취소 포함) 끝까지 간 뒤 `CancelledError`를 던진다.**
  git 변경 도중의
  취소는 프로세스 그룹을 죽여 `.git/index.lock`을 남기기 때문이다. 오래된 `index.lock`이 남아
  되돌리기가 실패하면 한 번 지우고 다시 시도한다. 그래도 남은 정리 태스크(drain 한도를 넘긴 CLI)가
  그 뒤에 쓰는 것은 막을 수 없으나, 호출자도 취소로 끝나므로 커밋하지 않고 다음 실행이 데이터
  사본을 `git clean`으로 새로 맞춘다(G9).
- `GeneratorExit`(코루틴을 그냥 닫음)는 기다릴 수 없으므로 그대로 던진다(`git/client.py`와 같다).
  정리 drain 도중 닫히면 되돌리기를 작업으로 예약만 하고 던진다.

알려진 한계(계획 2 Task 14·15 참고): `.git/HEAD.lock` 등 인덱스 말고 다른 락이 취소로 남는 경우는
복구하지 않는다(`index.lock`만 복구). 이 모듈 밖에서 `commit_all`·`reset` 같은 git **변경**을
직접 부르는 단계(Task 14·15)는 그 호출을 스스로 `run_to_completion`으로 감싸야 한다 — 변경 도중
취소는 프로세스 그룹을 죽여 락을 남긴다.
"""

import asyncio
import logging
import os
import stat
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from team_wiki_engine.claude.types import ClaudeRunner, RunClaudeInput, RunClaudeResult, Usage
from team_wiki_engine.git.client import GitClient, GitError
from team_wiki_engine.git.ops import changed_paths, clone_git_dir
from team_wiki_engine.ops.notify import redact_secrets
from team_wiki_engine.sync.post_check import (
    check_write_root,
    discard_work_tree,
    repair_and_discard,
    verify_claude_changes,
)
from team_wiki_engine.util.aio import run_to_completion, settle
from team_wiki_engine.wiki.lint_secrets import clean, safe_text

logger = logging.getLogger(__name__)

WriteStepStatus = Literal[
    "ok", "claude_failed", "post_check_failed", "unsafe_write_root", "unsettled"
]
Drain = Callable[[float], Awaitable[int]]

UNSETTLED_MESSAGE = "정리되지 않은 Claude 실행이 남아 커밋하지 않음"
DIRTY_MESSAGE = "작업 사본에 커밋되지 않은 변경이 있습니다"
_DETAIL_CHARS = 300
_MAX_MESSAGE_CHARS = 1000


@dataclass(frozen=True)
class WriteStep:
    """쓰기 실행 한 번의 결과. `run`은 `unsafe_write_root`일 때만 `None`이다."""

    status: WriteStepStatus
    run: RunClaudeResult | None
    offending: tuple[str, ...] = ()
    message: str = ""


def claude_failure_message(r: RunClaudeResult, redact: Sequence[str] = ()) -> str:
    """운영 채널에 보낼 실패 메시지(G6). `detail`은 비밀값을 가리고 300자로 자른다.

    알려진 비밀값(`redact`)은 **자르기 전에** 가린다(경계에 걸친 조각이 남지 않게).
    """
    message = f"Claude {r.failure or 'error'}"
    if r.detail:
        detail = safe_text(redact_secrets(r.detail, redact), _DETAIL_CHARS)
        if len(detail) > _DETAIL_CHARS:  # `clean`이 이스케이프로 늘린 경우까지 상한을 지킨다
            detail = f"{detail[:_DETAIL_CHARS]}…"
        message += ": " + detail
    return message


def _cap(text: str, redact: Sequence[str] = ()) -> str:
    text = clean(redact_secrets(text, redact))
    return text if len(text) <= _MAX_MESSAGE_CHARS else f"{text[:_MAX_MESSAGE_CHARS]}…"


def _raised_result(exc: Exception) -> RunClaudeResult:
    """러너가 던진 예외를 실패 결과로 바꾼다. 예외 문구는 싣지 않는다(비밀값 가능성)."""
    return RunClaudeResult(
        ok=False,
        text="",
        session_id=None,
        cost_usd=0.0,
        usage=Usage(),
        duration_ms=0,
        tool_calls=[],
        failure="error",
        detail=f"실행기가 예외를 던졌습니다: {type(exc).__name__}",
    )


def _remove_stale_index_lock(git: GitClient) -> bool:
    """우리 clone의 `.git/index.lock`이 일반 파일이면 지운다(링크는 따라가지 않는다).

    clone 가드(`clone_git_dir`)를 먼저 통과해야 한다 — 작업 사본이 링크이거나 `.git`·`HEAD`가
    이상하면 다른 저장소의 락을 지우지 않도록 아무것도 하지 않는다.
    """
    try:
        dot_git = clone_git_dir(git)
        lock = os.path.join(dot_git, "index.lock")
        if not stat.S_ISREG(os.lstat(lock).st_mode):
            return False
        os.unlink(lock)
    except Exception:
        return False
    return True


def _schedule_discard(git: GitClient) -> None:
    """코루틴이 닫히는 중이라 기다릴 수 없을 때 되돌리기를 작업으로 예약한다."""
    coro = discard_quietly(git)
    try:
        task = asyncio.ensure_future(coro)
    except RuntimeError:  # 돌고 있는 이벤트 루프가 없다
        coro.close()
        return
    task.add_done_callback(lambda t: t.cancelled() or t.exception())


async def discard_quietly(git: GitClient) -> None:
    """작업 사본을 되돌린다. 실패는 기록만 하고 삼킨다.

    취소가 몇 번 와도 되돌리기는 끝까지 간 뒤 `CancelledError`를 던진다. 되돌리기 태스크 자체가
    취소로 끝났으면 성공으로 세지 않고 한 번 다시 시도한다(그래도 취소면 취소를 던진다).
    `GitError`로 실패했으면(`CloneStateError`는 해당 없음) 오래된 `index.lock`을 지우고 한 번
    다시 시도한다(동기화 잠금 아래의 우리 clone이다).
    """
    cancelled = False
    for attempt in (1, 2):
        task, was_cancelled = await settle(discard_work_tree(git))
        cancelled = cancelled or was_cancelled
        if task.cancelled():
            if attempt == 1:
                continue  # 끝나지 않았다 — 성공이 아니다
            cancelled = True
            break
        error = task.exception()
        if error is None:
            break
        if attempt == 1 and isinstance(error, GitError) and _remove_stale_index_lock(git):
            logger.warning("오래된 index.lock을 지우고 되돌리기를 다시 시도합니다")
            continue
        logger.warning("작업 사본 되돌리기에 실패했습니다: %s", type(error).__name__)
        break
    if cancelled:
        raise asyncio.CancelledError


def _same_dir(a: object, b: object) -> bool:
    try:
        return os.path.samefile(str(a), str(b))
    except OSError:
        return False


async def run_write_step(
    *,
    runner: ClaudeRunner,
    inp: RunClaudeInput,
    git: GitClient,
    data_dir: Path,
    drain: Drain,
    settle_timeout_s: float,
    redact: Sequence[str] = (),
) -> WriteStep:
    if not (_same_dir(inp.cwd, git.cwd) and _same_dir(data_dir, git.cwd)):
        return WriteStep("unsafe_write_root", None, message="경로가 서로 다릅니다")
    reason = check_write_root(data_dir, redact)
    if reason is not None:
        return WriteStep("unsafe_write_root", None, message=_cap(reason, redact))
    # `status`는 취소돼도 락을 남기지 않게 끝까지 돌린다(`--no-optional-locks`와 이중).
    if await run_to_completion(changed_paths(git)):  # 무시된 파일도 센다
        return WriteStep("unsafe_write_root", None, message=DIRTY_MESSAGE)

    drain_started = False
    try:
        try:
            r = await runner(inp)
        except Exception as exc:
            r = _raised_result(exc)

        drain_started = True
        try:
            pending = await drain(settle_timeout_s)
            unsettled = pending > 0
        except Exception:
            unsettled = True  # 정리 여부를 알 수 없다
        if unsettled:
            await repair_and_discard(git)
            return WriteStep("unsettled", r, message=UNSETTLED_MESSAGE)

        if not r.ok and r.failure != "empty":
            await repair_and_discard(git)
            return WriteStep(
                "claude_failed", r, message=_cap(claude_failure_message(r, redact), redact)
            )

        check = await verify_claude_changes(git, redact)  # 위반이면 그 안에서 되돌린다
        if not check.ok:
            offending = check.offending
            message = _cap("허용 경로 밖 변경: " + ", ".join(offending), redact)
            return WriteStep("post_check_failed", r, offending, message)
        return WriteStep("ok", r)
    except GeneratorExit:
        raise  # 코루틴이 그냥 닫힌다: 기다릴 수 없다
    except BaseException:
        closing = False
        try:
            if not drain_started:  # 취소·KeyboardInterrupt: 버려진 실행의 정리를 한 번 기다린다
                drain_started = True
                try:
                    await drain(settle_timeout_s)
                except Exception:
                    pass
        except GeneratorExit:
            closing = True  # 닫히는 중에는 기다릴 수 없다
            raise
        finally:
            if closing:
                _schedule_discard(git)
            else:
                await discard_quietly(git)  # drain 중 두 번째 취소가 와도 반드시 되돌린다
        raise
