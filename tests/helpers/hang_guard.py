"""멈출 수 있는 코드를 감싸는 시간 제한(`SIGALRM`). 퇴행하면 멈추는 대신 실패한다."""

import signal
from collections.abc import Iterator
from contextlib import contextmanager
from types import FrameType


class HangError(AssertionError):
    pass


@contextmanager
def hang_guard(seconds: int = 5) -> Iterator[None]:
    def on_alarm(signum: int, frame: FrameType | None) -> None:
        raise HangError(f"{seconds}초 안에 끝나지 않았습니다")

    previous = signal.signal(signal.SIGALRM, on_alarm)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def open_fd_count() -> int:
    """이 프로세스가 연 파일 서술자 수(`/dev/fd`는 macOS·리눅스 모두 있다)."""
    import os

    return len(os.listdir("/dev/fd"))
