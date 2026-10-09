"""데이터 저장소 사본 준비(G4·G9). 동기화의 `data-sync`와 봇의 `data`가 함께 쓴다.

`prepare_data_copy`는 사본을 `origin/<base>`와 **똑같이** 맞춘다. 서버 사본에는 사람이 고친 내용이
없으므로 로컬 수정·추적 안 된 파일·무시된 파일(`.DS_Store`)·로컬에만 있는 커밋을 모두 버린다.
예전 clone이 풀어 둔 심볼릭 링크도 일반 파일로 바꾼다(`disable_symlinks`).

호출자는 이 사본에 대한 **배타적 권한**(Task 12의 `acquire_lock`)을 쥐고 있어야 한다. 이 함수는
같은 사본 폴더를 동시에 만지는 다른 실행을 막지 않는다.

공유 도우미: 이 모듈의 `clone_fresh`(조각 폴더 + 원자적 rename)·`is_fresh`·`sweep_partials`·
`clear_children`·`remove_stale_git_locks`는 코드 사본(`sync/repos.py`)도 같은 규칙으로 쓴다. 코드
사본 쪽 호출은 사본 자리가 `repos/<이름>`이고 `git`이 코드 사본용 템플릿이라는 점만 다르다.

취소와 끊긴 clone:
- **새 clone**은 `<부모>/.<이름>.partial-<pid>-<토큰>` 조각 폴더에 받고, 다 받은 뒤에만
  `os.rename`으로 사본 자리에 놓는다(같은 폴더라 원자적이다). 도중에 취소·실패·프로세스 종료가
  와도 사본 자리는 비어 있거나 없는 채로 남는다. 그래서 다음 실행이 "`.git`은 없는데 폴더가 비어
  있지 않다"(`CloneStateError`)로 영원히 막히는 일이 없다. 취소·실패는 조각을 바로 지운다.
  `SIGKILL`·정전으로 남은 조각은 **다음 실행이 시작할 때** 지운다. 이름이 같은 접두어에
  `<pid>-<토큰>` 모양이고, 그 pid가 죽었거나 이 프로세스의 지난 호출 것일 때만 지운다(살아 있는
  다른 프로세스의 조각은 건드리지 않는다). 링크는 따라가지 않고 링크 자체만 지운다. clone은 조각
  안에서만 일어나므로 취소하면 바로 멈춘다(`run_to_completion`으로 감싸지 않는다).
- **기존 사본을 바꾸는 작업**은 단계마다 보호가 다르다. 설정 쓰기와 `remote set-url`, 그리고
  `checkout -B`·`clean`(`reset_branch_to`)은 `run_to_completion` 안에서 끝까지 돈다. 도중에
  프로세스 그룹을 `SIGKILL`하면 반쯤 바뀐 작업 사본이 남기 때문이다. `fetch`는 보호하지 않는다.
  ref를 원자적으로 갱신하므로 죽여도 사본이 깨지지 않아 취소가 바로 전달되고(최대 600초를
  기다리지 않는다), 남는 ref 잠금은 아래 회복이 치운다.
- **남은 `.git/*.lock` 회복.** `SIGKILL`·정전·OOM으로 git이 죽으면 `index.lock`·`HEAD.lock`·
  `config.lock`·`packed-refs.lock`·`shallow.lock`·`objects/maintenance.lock`·`refs/heads/<base>.lock`·
  `refs/remotes/origin/<base>.lock`이 남아 이후 모든 실행이 영원히 실패한다. 기존 사본 동기화가
  `GitError`(종류 `exit`)로 실패하면 사본 가드(`clone_git_dir`)를 통과한 경우에만 **그 정확한
  경로들 중 일반 파일**을 지우고(링크·폴더·FIFO는 건드리지 않고, 경로의 어느 구성요소도 링크를
  따라가지 않는다) 한 번만 다시 시도한다. 그래도 실패하면 처음 오류를 던진다. 경고 로그에는 지운
  개수만 남는다. 이는 **단일 작성자(동기화 `flock`)를 전제**로 안전하다: 실행 중인 다른 git의
  잠금과 구별할 수 없다.
- 주의: `flock`은 `SIGKILL`된 홀더가 남긴 자식에게는 미치지 않는다. git은 자기 세션(프로세스
  그룹)에서 돌기 때문에 홀더가 죽어도 살아 남을 수 있다. systemd 단위(Task 17)는
  `KillMode=control-group`을 유지해 홀더와 함께 모두 정리해야 한다.
"""

import errno
import logging
import os
import re
import secrets
import shutil
import stat
from pathlib import Path

from team_wiki_engine.git.client import GitClient, GitError
from team_wiki_engine.git.ops import (
    CloneStateError,
    clone_git_dir,
    disable_symlinks,
    ensure_clone,
    fetch_branch,
    is_safe_branch,
    remote_url,
    reset_branch_to,
    set_remote_url,
)
from team_wiki_engine.util.aio import run_to_completion

_log = logging.getLogger(__name__)
_MAX_PID = 2**31 - 1
_PARTIAL_SUFFIX = re.compile(r"([0-9]+)-([0-9A-Za-z]{1,32})")
_in_flight: set[str] = set()  # 이 프로세스에서 지금 받고 있는 조각 폴더 이름


async def prepare_data_copy(git: GitClient, url: str, base: str) -> None:
    """`git.cwd`의 사본을 `url`의 `base` 브랜치(`origin/<base>`)와 똑같이 맞춘다.

    `base`가 안전한 브랜치 이름이 아니면 `ValueError`, `url`이 안전하지 않아도 `ValueError`
    (git을 부르기 전). 사본 폴더가 링크·`.git` 없는 채워진 폴더·가짜 저장소면 `CloneStateError`.
    원격에 `base`가 없거나 git이 실패하면 `GitError`.
    """
    if not isinstance(base, str) or not is_safe_branch(base):
        raise ValueError("브랜치 이름 형식이 올바르지 않습니다")
    cwd = Path(os.path.abspath(git.cwd))
    sweep_partials(cwd)
    if is_fresh(cwd):
        await clone_fresh(git, url, cwd)
    else:
        await ensure_clone(git, url)  # 이미 저장소면 그대로, 링크·채워진 폴더면 CloneStateError
    try:
        await _sync_existing(git, url, base)
    except GitError as exc:
        if exc.kind != "exit":
            raise
        removed = remove_stale_git_locks(git, base)
        if not removed:
            raise
        _log.warning("데이터 사본에 남은 git 잠금 파일 %d개를 지우고 다시 시도합니다", removed)
        try:
            await _sync_existing(git, url, base)
        except GitError:
            raise exc from None  # 처음 오류를 그대로(git 오류 메시지는 이미 비밀값이 가려져 있다)


async def _prepare_config(git: GitClient, url: str) -> None:
    await disable_symlinks(git)
    if await remote_url(git) != url:
        await set_remote_url(git, url)


async def _sync_existing(git: GitClient, url: str, base: str) -> None:
    await run_to_completion(_prepare_config(git, url))
    await fetch_branch(git, base)  # 일부러 보호하지 않는다: 죽여도 사본이 깨지지 않는다
    await run_to_completion(reset_branch_to(git, base, f"origin/{base}"))


# ---------------------------------------------------------------- 남은 git 잠금 회복

_TOP_LOCKS = (
    "index.lock",
    "HEAD.lock",
    "config.lock",
    "packed-refs.lock",
    "shallow.lock",
    "objects/maintenance.lock",
)


def remove_stale_git_locks(git: GitClient, base: str) -> int:
    """사본 가드를 통과하면 알려진 `.git/*.lock` **일반 파일**만 지운다. 지운 개수를 돌려준다.

    가드(`clone_git_dir`)가 실패하면(링크 사본·이상한 `HEAD`·…) 아무것도 지우지 않는다. 경로는
    `.git`에서 `O_NOFOLLOW`로 폴더를 한 단계씩 열어 내려가므로 중간이 링크이면 따라가지 않고,
    마지막 항목은 `lstat`으로 일반 파일일 때만 `unlink`한다. 단일 작성자(동기화 `flock`) 전제.
    """
    if not is_safe_branch(base):
        return 0
    try:
        dot_git = clone_git_dir(git)
    except CloneStateError:
        return 0
    names = [*_TOP_LOCKS, f"refs/heads/{base}.lock", f"refs/remotes/origin/{base}.lock"]
    return sum(_unlink_regular(dot_git, name) for name in names)


def _unlink_regular(root: str, relative: str) -> int:
    *directories, leaf = relative.split("/")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fds: list[int] = []
    try:
        fds.append(os.open(root, flags))
        for part in directories:
            fds.append(os.open(part, flags, dir_fd=fds[-1]))
        if not stat.S_ISREG(os.stat(leaf, dir_fd=fds[-1], follow_symlinks=False).st_mode):
            return 0
        os.unlink(leaf, dir_fd=fds[-1])
        return 1
    except OSError:
        return 0
    finally:
        for fd in fds:
            os.close(fd)


# ---------------------------------------------------------------- 새 clone


def is_fresh(cwd: Path) -> bool:
    """사본 자리가 없거나 **실제** 빈 폴더인가(링크·파일·채워진 폴더·읽을 수 없는 폴더는 아니다)."""
    try:
        mode = os.lstat(cwd).st_mode
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if not stat.S_ISDIR(mode):
        return False
    try:
        with os.scandir(cwd) as entries:
            return next(entries, None) is None
    except OSError:
        return False


async def clone_fresh(git: GitClient, url: str, cwd: Path) -> None:
    """`cwd`(없거나 빈 폴더 — `is_fresh`)에 `url`을 clone한다. 코드 사본(`sync/repos.py`)도 쓴다.

    같은 폴더의 조각 폴더에 받고 다 받은 뒤에만 `os.rename`으로 `cwd`에 놓는다. 취소·실패하면
    조각을 지우고 `cwd`는 처음 그대로다. `git.cwd`가 아니라 **`cwd`** 가 놓일 자리이며 `git`은
    인증·전송 제한을 빌려 주는 템플릿이다(`git.at(조각)`으로 쓴다).
    """
    partial = cwd.with_name(f".{cwd.name}.partial-{os.getpid()}-{secrets.token_hex(4)}")
    _in_flight.add(partial.name)
    try:
        await ensure_clone(git.at(partial), url)
        try:
            os.rename(partial, cwd)  # 같은 폴더 안: 원자적. 빈 폴더 위로는 덮어쓴다
        except OSError as exc:
            if not is_fresh(cwd):
                raise CloneStateError(_rename_message(exc)) from None
            # 사본 자리가 여전히 비어 있다(마운트 지점 EBUSY·EXDEV·권한 등): 제자리에서 받는다.
            _remove(partial)
            await _clone_in_place(git, url, cwd)
    except BaseException:
        _remove(partial)  # 취소·실패 모두 조각을 남기지 않는다(동기 호출이라 취소에 끊기지 않는다)
        raise
    finally:
        _in_flight.discard(partial.name)


def _rename_message(exc: OSError) -> str:
    if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
        return "사본 자리를 비울 수 없어 사본을 놓지 못했습니다(폴더가 그 사이 채워짐)"
    return (
        f"사본을 사본 자리로 옮기지 못했습니다({errno.errorcode.get(exc.errno or 0, '알 수 없음')})"
    )


async def _clone_in_place(git: GitClient, url: str, cwd: Path) -> None:
    """비어 있는 사본 자리에 바로 받는다. 실패·취소하면 자리를 다시 비운다(원래 비어 있었다)."""
    try:
        await ensure_clone(git, url)
    except BaseException:
        clear_children(cwd)
        raise


def clear_children(directory: Path) -> None:
    """`directory`의 **최상위 항목을 모두** 지운다(폴더 자신은 남긴다). 코드 사본이 끊긴 옛 clone을
    다시 받기 전에 쓴다. 링크는 따라가지 않고 링크만 지우며 실패해도 던지지 않는다.
    """
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        _remove(directory / name)


def _make_walkable(path: Path) -> None:
    """읽기 전용·접근 불가 하위 폴더를 지울 수 있게 `u+rwx`로 연다. 링크는 따라가지 않는다."""
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            if not stat.S_ISDIR(os.lstat(current).st_mode):
                continue
            os.chmod(current, 0o700)
            with os.scandir(current) as entries:
                stack.extend(
                    Path(entry.path) for entry in entries if entry.is_dir(follow_symlinks=False)
                )
        except OSError:
            continue


def _remove(path: Path) -> None:
    """링크는 따라가지 않고 그 자체만 지운다. 실패해도 던지지 않는다(다음 실행이 다시 치운다)."""
    try:
        mode = os.lstat(path).st_mode
    except OSError:
        return
    try:
        if stat.S_ISDIR(mode):
            _make_walkable(path)
            shutil.rmtree(path, ignore_errors=True)
        else:
            os.unlink(path)
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    if not 1 <= pid <= _MAX_PID:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return (
            True  # PermissionError(= 다른 사용자의 살아 있는 프로세스) 등은 살아 있는 것으로 본다
        )
    return True


def sweep_partials(cwd: Path) -> None:
    """이 사본의 이름으로 남은 옛 조각 폴더를 치운다. 실패해도 던지지 않는다."""
    prefix = f".{cwd.name}.partial-"
    try:
        names = os.listdir(cwd.parent)
    except OSError:
        return
    for name in names:
        if not name.startswith(prefix):
            continue
        match = _PARTIAL_SUFFIX.fullmatch(name[len(prefix) :])
        if match is None:
            continue
        pid = int(match.group(1)) if len(match.group(1)) <= 12 else 0
        if pid == os.getpid():
            if name in _in_flight:
                continue
        elif _pid_alive(pid):
            continue
        _remove(cwd.parent / name)
