"""`fcntl.flock` 실행 잠금(P10).

한 번에 한 동기화만 돈다. 규칙:
- 잠금은 파일 **내용이 아니라 커널의 `flock`**이다. 프로세스가 어떻게 죽어도(`SIGKILL` 포함)
  커널이 풀어 주므로 죽은 잠금이 남지 않고 PID 재사용 경합도 없다. 다음 `acquire_lock`이 그냥
  잡는다.
- 잠금 파일은 **절대 지우지 않는다**. 지우면 기다리던 쪽이 옛 inode를, 새로 만든 쪽이 새
  inode를 각각 잡아 두 실행이 동시에 도는 경합이 생긴다. `release()`도 내용만 비운다.
- `flock`은 열린 파일 기술(open file description)마다 걸린다. 같은 프로세스에서 같은 경로를
  두 번 `open`하면 서로 충돌하므로 두 번째 `acquire_lock`은 `None`이다(리눅스·macOS 모두).
- 파일은 `O_NOFOLLOW`로 연다: 마지막 구성요소가 링크이면 `LockError`이고 링크 대상은 만들거나
  고치지 않는다(조상 경로의 링크는 따라간다 — 상태 폴더 위치는 설정이 정한다). `O_CLOEXEC`라
  git·Claude CLI 같은 자식 프로세스가 fd를 물려받아 부모가 죽은 뒤에도 잠금을 쥐고 있을 수
  없다. `O_NONBLOCK`으로 열어(아래) FIFO·장치가 열기에서 막히지 않게 하고, 연 뒤 일반 파일인지
  `fstat`으로 확인해 아니면 `LockError`다(FIFO·장치·폴더에 `flock`을 걸고 내용을 쓰지 않는다).
- 열 때 `O_NONBLOCK|O_NOCTTY`도 준다: tty·직렬 장치가 열기에서 막히거나 제어 터미널이 되지 않게 한다
  (연 뒤 일반 파일이 아니면 곧바로 `LockError`). 하드링크된 파일(`st_nlink != 1`)도 내용을 비우거나
  권한을 바꾸기 **전에** `LockError`다(다른 파일의 내용을 비우는 길이 되지 않게).
- **상태 폴더는 로컬 파일 시스템이어야 한다.** NFS 같은 네트워크 파일 시스템에서는 `flock`의
  의미(커널이 프로세스 종료 때 풀어 줌, 호스트 사이 배타성)가 보장되지 않는다.
- `exec` 없이 `fork`한 자식은 fd와 함께 잠금을 물려받아 부모가 죽은 뒤에도 쥐고 있다. 그래서
  자식에서 `release()`·소멸자는 fd만 닫고 **잠금을 풀거나 내용을 비우지 않는다**(공유된 잠금을
  부모 몰래 풀지 않게 — 잠금 취득 때 기록한 pid와 다르면 자식이다). 우리 코드는 `fork`하지 않는다.
- 권한은 0600이다(이미 있던 파일이 넓은 권한이면 소유자일 때 좁힌다).
- 내용(`pid=<pid> started=<KST ISO>\\n`)은 **진단용**이다. 잠금을 쥔 뒤 `ftruncate` + `pwrite`로
  쓰고 잠금 판정에는 쓰지 않는다. 죽은 프로세스가 남긴 내용은 다음 주인이 잠금을 쥘 때 덮어쓰고
  놓을 때 비운다. 따라서 `read_lock_owner`는 **`acquire_lock`이 `None`을 돌려준 직후**(= 누군가
  지금 쥐고 있음)에만 의미가 있다. 쥔 직후 내용을 쓰기 전의 짧은 틈에는 빈 필드가 읽힌다.
- `LockError` 메시지에는 경로·`errno` 문구·파일 내용을 싣지 않는다.
- 스레드를 쓰지 않는다. `LOCK_NB`라 이벤트 루프를 막지 않으므로 코루틴에서 그대로 부른다.
"""

import errno
import fcntl
import os
import re
import stat
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Self

from team_wiki_engine.util.time import kst_iso, parse_instant

LONG_HELD = timedelta(hours=6)

# 링크에 O_NOFOLLOW를 쓰면 리눅스·macOS는 ELOOP, 일부 BSD는 EMLINK를 낸다.
_LINK_ERRNOS = frozenset({errno.ELOOP, errno.EMLINK})
_READ_LIMIT = 4096
_MAX_PID_DIGITS = 10
_MAX_PID = 2**31 - 1
_PID_FIELD = re.compile(r"[0-9]{1,10}")


class LockError(Exception):
    """잠금 파일을 안전하게 열거나 잡을 수 없다(링크·폴더 생성 실패·권한 등). 내용 없는 메시지."""


@dataclass(frozen=True)
class LockOwner:
    pid: int | None
    started_at: datetime | None


class SyncLock:
    """잡은 잠금. `release()`를 부르거나 `with`를 빠져나오면 풀린다(여러 번 불러도 된다)."""

    def __init__(self, path: Path, fd: int) -> None:
        self.path = path
        self._fd: int | None = fd
        self._owner_pid = os.getpid()
        self._mutex = threading.Lock()  # 한 fd를 두 번 닫아 재사용된 다른 fd를 닫는 일을 막는다

    @property
    def held(self) -> bool:
        return self._fd is not None

    def release(self) -> None:
        with self._mutex:
            fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            if os.getpid() == self._owner_pid:  # fork만 한 자식은 공유된 잠금을 풀지 않는다
                try:
                    os.ftruncate(
                        fd, 0
                    )  # 쥔 채로 비운다(놓은 뒤에 비우면 다음 주인의 내용을 지운다)
                except OSError:
                    pass
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            try:
                os.close(fd)  # 닫으면 어차피 커널이 푼다
            except OSError:
                pass

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()

    def __del__(self) -> None:
        # 놓지 않고 버려진 잠금이 프로세스가 끝날 때까지 남지 않게 한다.
        try:
            self.release()
        except Exception:  # noqa: BLE001 - 소멸자는 던지지 않는다
            pass

    def __repr__(self) -> str:
        return f"SyncLock(held={self.held})"


def _open_lock_file(file: Path) -> int:
    try:
        file.parent.mkdir(parents=True, exist_ok=True)
    except (OSError, ValueError):
        raise LockError("잠금 폴더를 만들 수 없습니다") from None
    try:
        fd = os.open(
            file,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOCTTY,
            0o600,
        )
    except (OSError, ValueError) as exc:
        if isinstance(exc, OSError) and exc.errno in _LINK_ERRNOS:
            raise LockError("잠금 파일이 링크라서 열지 않습니다") from None
        raise LockError("잠금 파일을 열 수 없습니다") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise LockError("잠금 파일이 일반 파일이 아닙니다")
        if info.st_nlink != 1:
            raise LockError("잠금 파일이 하드링크라서 쓰지 않습니다")
        if stat.S_IMODE(info.st_mode) & 0o077 and info.st_uid == os.geteuid():
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                pass
    except BaseException:
        os.close(fd)
        raise
    return fd


def acquire_lock(file: Path, *, now: datetime) -> SyncLock | None:
    """잠금을 잡아 돌려준다. 이미 누가 쥐고 있으면 `None`. 파일을 못 열면 `LockError`."""
    fd = _open_lock_file(file)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except OSError:
        os.close(fd)
        raise LockError("잠금을 걸 수 없습니다") from None
    lock = SyncLock(file, fd)
    try:
        data = f"pid={os.getpid()} started={kst_iso(now)}\n".encode()
        os.ftruncate(fd, 0)
        written = 0
        while written < len(data):
            written += os.pwrite(fd, data[written:], written)
    except (OSError, ValueError, OverflowError):
        lock.release()
        raise LockError("잠금 내용을 쓸 수 없습니다") from None
    return lock


def read_lock_owner(file: Path) -> LockOwner:
    """잠금 파일 앞 4KB에서 `pid=`·`started=`를 읽는다. 없거나 깨졌으면 `None` 필드. 예외 없음."""
    empty = LockOwner(None, None)
    try:
        fd = os.open(file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except (OSError, ValueError):
        return empty
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return empty
        raw = os.read(fd, _READ_LIMIT)
    except OSError:
        return empty
    finally:
        os.close(fd)
    return _parse_owner(raw)


def _parse_owner(raw: bytes) -> LockOwner:
    first_line = raw.split(b"\n", 1)[0].decode("utf-8", errors="replace")
    pid: int | None = None
    started: datetime | None = None
    seen: set[str] = set()
    for token in first_line.split(" "):
        key, sep, value = token.partition("=")
        if not sep or key in seen:
            continue
        seen.add(key)
        if key == "pid":
            if _PID_FIELD.fullmatch(value):
                number = int(value)
                if 1 <= number <= _MAX_PID:
                    pid = number
        elif key == "started":
            started = parse_instant(value)
    return LockOwner(pid, started)


def held_too_long(owner: LockOwner, now: datetime) -> bool:
    """`started_at`이 있고 `now`와의 차이가 `LONG_HELD`를 넘으면 참."""
    if owner.started_at is None:
        return False
    started = _aware(owner.started_at)
    try:
        return _aware(now) - started > LONG_HELD
    except OverflowError:
        return False


def _aware(value: datetime) -> datetime:
    """naive 시각은 UTC로 본다(`parse_instant`·`kst_iso`와 같은 규칙)."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value
