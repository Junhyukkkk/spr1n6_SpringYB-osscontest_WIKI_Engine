"""`flock` 잠금(P10).

다른 프로세스 시험은 `sys.executable -c`로 띄운 작은 스크립트가 잠금을 쥔다. 이벤트 루프를
막지 않도록 `asyncio` 하위 프로세스로 돌리고, 모든 대기는 `asyncio.timeout` 안에서 한다.
"""

import asyncio
import dataclasses
import gc
import os
import signal
import stat
import sys
import textwrap
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio

from team_wiki_engine.sync.lock import (
    LONG_HELD,
    LockError,
    LockOwner,
    SyncLock,
    acquire_lock,
    held_too_long,
    read_lock_owner,
)

NOW = datetime(2026, 10, 7, 0, 0, 0, tzinfo=UTC)  # = 2026-10-07T09:00:00+09:00

_FOREIGN_HOLDER = textwrap.dedent(
    """
    import fcntl, os, sys, time
    fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    print("ready", flush=True)
    time.sleep(30)
    """
)

# 우리 모듈로 잠금을 쥐고, 그 상태에서 fd를 상속시킬 수 있는 가장 헐거운 방식(close_fds=False)으로
# 손자 프로세스를 띄운다. 손자는 부모(홀더)가 죽어도 살아 있다.
_HOLDER_WITH_CHILD = textwrap.dedent(
    """
    import subprocess, sys, time
    from datetime import UTC, datetime
    from pathlib import Path
    from team_wiki_engine.sync.lock import acquire_lock
    lock = acquire_lock(Path(sys.argv[1]), now=datetime.now(UTC))
    assert lock is not None
    child = subprocess.Popen(
        ["sleep", "30"],
        close_fds=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(child.pid, flush=True)
    time.sleep(30)
    """
)


async def _spawn(script: str, lock_file: Path) -> asyncio.subprocess.Process:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        str(lock_file),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    return proc


async def _first_line(proc: asyncio.subprocess.Process) -> str:
    assert proc.stdout is not None
    async with asyncio.timeout(20):
        line = await proc.stdout.readline()
    if not line:
        assert proc.stderr is not None
        err = await proc.stderr.read()
        pytest.fail(f"하위 프로세스가 준비 신호 없이 끝났습니다: {err.decode(errors='replace')}")
    return line.decode().strip()


async def _kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        proc.kill()
    async with asyncio.timeout(10):
        await proc.wait()


@pytest_asyncio.fixture
async def foreign(tmp_dir: Path) -> AsyncIterator[tuple[asyncio.subprocess.Process, Path]]:
    file = tmp_dir / "state" / "sync.lock"
    file.parent.mkdir()
    proc = await _spawn(_FOREIGN_HOLDER, file)
    try:
        assert await _first_line(proc) == "ready"
        yield proc, file
    finally:
        await _kill(proc)


class TestAcquire:
    def test_부모_폴더를_만들고_잠금을_잡는다(self, tmp_dir: Path) -> None:
        file = tmp_dir / "state" / "deep" / "sync.lock"
        lock = acquire_lock(file, now=NOW)
        assert lock is not None
        try:
            assert lock.held
            assert lock.path == file
            assert file.is_file()
        finally:
            lock.release()

    def test_같은_프로세스의_두_번째_잠금은_None이고_놓으면_다시_잡힌다(
        self, tmp_dir: Path
    ) -> None:
        file = tmp_dir / "sync.lock"
        first = acquire_lock(file, now=NOW)
        assert first is not None
        assert acquire_lock(file, now=NOW) is None
        # 실패한 시도가 첫 잠금을 풀거나 내용을 지우지 않는다.
        assert first.held
        assert read_lock_owner(file).pid == os.getpid()
        first.release()
        second = acquire_lock(file, now=NOW)
        assert second is not None
        second.release()

    def test_놓은_뒤에도_파일은_남고_내용은_비어_있다(self, tmp_dir: Path) -> None:
        file = tmp_dir / "sync.lock"
        lock = acquire_lock(file, now=NOW)
        assert lock is not None
        inode = file.stat().st_ino
        lock.release()
        assert file.exists()
        assert file.read_bytes() == b""
        again = acquire_lock(file, now=NOW)
        assert again is not None
        assert file.stat().st_ino == inode  # 지웠다 다시 만들지 않는다(경합 방지)
        again.release()

    def test_release는_여러_번_불러도_된다(self, tmp_dir: Path) -> None:
        lock = acquire_lock(tmp_dir / "sync.lock", now=NOW)
        assert lock is not None
        lock.release()
        assert not lock.held
        lock.release()
        lock.release()
        assert not lock.held

    def test_release_뒤에_다른_잠금을_지우지_않는다(self, tmp_dir: Path) -> None:
        file = tmp_dir / "sync.lock"
        old = acquire_lock(file, now=NOW)
        assert old is not None
        old.release()
        new = acquire_lock(file, now=NOW)
        assert new is not None
        old.release()  # 이미 놓은 잠금의 두 번째 release는 새 주인의 잠금·내용에 손대지 않는다
        assert new.held
        assert acquire_lock(file, now=NOW) is None
        assert read_lock_owner(file).pid == os.getpid()
        new.release()

    def test_컨텍스트_매니저가_놓아_준다(self, tmp_dir: Path) -> None:
        file = tmp_dir / "sync.lock"
        lock = acquire_lock(file, now=NOW)
        assert lock is not None
        with lock as inside:
            assert inside is lock
            assert inside.held
            assert acquire_lock(file, now=NOW) is None
        assert not lock.held
        again = acquire_lock(file, now=NOW)
        assert again is not None
        again.release()

    def test_예외가_나도_컨텍스트가_놓아_준다(self, tmp_dir: Path) -> None:
        file = tmp_dir / "sync.lock"
        lock = acquire_lock(file, now=NOW)
        assert lock is not None
        with pytest.raises(RuntimeError), lock:
            raise RuntimeError("boom")
        assert not lock.held
        assert acquire_lock(file, now=NOW) is not None

    def test_내용은_pid와_KST_시작_시각이다(self, tmp_dir: Path) -> None:
        file = tmp_dir / "sync.lock"
        lock = acquire_lock(file, now=NOW)
        assert lock is not None
        try:
            assert file.read_text(encoding="utf-8") == (
                f"pid={os.getpid()} started=2026-10-07T09:00:00+09:00\n"
            )
        finally:
            lock.release()

    def test_예전_내용이_길어도_남지_않는다(self, tmp_dir: Path) -> None:
        file = tmp_dir / "sync.lock"
        file.write_text("x" * 10_000, encoding="utf-8")
        lock = acquire_lock(file, now=NOW)
        assert lock is not None
        try:
            assert file.read_text(encoding="utf-8") == (
                f"pid={os.getpid()} started=2026-10-07T09:00:00+09:00\n"
            )
        finally:
            lock.release()

    def test_잠금_파일_권한은_0600이다(self, tmp_dir: Path) -> None:
        file = tmp_dir / "sync.lock"
        lock = acquire_lock(file, now=NOW)
        assert lock is not None
        try:
            assert stat.S_IMODE(file.stat().st_mode) == 0o600
        finally:
            lock.release()

    def test_넓은_권한의_예전_파일도_0600으로_좁힌다(self, tmp_dir: Path) -> None:
        file = tmp_dir / "sync.lock"
        file.write_text("", encoding="utf-8")
        file.chmod(0o666)
        lock = acquire_lock(file, now=NOW)
        assert lock is not None
        try:
            assert stat.S_IMODE(file.stat().st_mode) == 0o600
        finally:
            lock.release()

    def test_잠금이_잡혀_있는_동안_파일은_지워지지_않는다(self, tmp_dir: Path) -> None:
        file = tmp_dir / "sync.lock"
        lock = acquire_lock(file, now=NOW)
        assert lock is not None
        try:
            assert acquire_lock(file, now=NOW) is None
            assert file.exists()  # None을 돌려준 쪽도 파일을 지우지 않는다
        finally:
            lock.release()

    def test_SyncLock은_dataclass가_아니고_repr에_경로_외_내용이_없다(self, tmp_dir: Path) -> None:
        lock = acquire_lock(tmp_dir / "sync.lock", now=NOW)
        assert lock is not None
        try:
            assert not dataclasses.is_dataclass(lock)
            assert isinstance(lock, SyncLock)
            assert "pid=" not in repr(lock)
        finally:
            lock.release()


class TestForeignProcess:
    async def test_다른_프로세스가_쥐고_있으면_None이다(
        self, foreign: tuple[asyncio.subprocess.Process, Path]
    ) -> None:
        _, file = foreign
        assert acquire_lock(file, now=NOW) is None
        assert file.exists()

    async def test_그_프로세스를_죽이면_바로_잡힌다(
        self, foreign: tuple[asyncio.subprocess.Process, Path]
    ) -> None:
        proc, file = foreign
        assert acquire_lock(file, now=NOW) is None
        proc.send_signal(signal.SIGKILL)
        async with asyncio.timeout(10):
            await proc.wait()
        lock = acquire_lock(file, now=NOW)  # 죽은 잠금 회수: 지우거나 PID를 확인하지 않는다
        assert lock is not None
        lock.release()

    async def test_잠금을_쥔_프로세스가_띄운_자식은_잠금을_물려받지_않는다(
        self, tmp_dir: Path
    ) -> None:
        file = tmp_dir / "sync.lock"
        holder = await _spawn(_HOLDER_WITH_CHILD, file)
        child_pid = 0
        try:
            child_pid = int(await _first_line(holder))
            assert acquire_lock(file, now=NOW) is None  # 홀더가 쥐고 있다
            holder.send_signal(signal.SIGKILL)
            async with asyncio.timeout(10):
                await holder.wait()
            os.kill(child_pid, 0)  # 손자는 아직 살아 있다
            lock = acquire_lock(file, now=NOW)  # 그래도 잠금은 홀더와 함께 풀렸다
            assert lock is not None
            lock.release()
        finally:
            await _kill(holder)
            if child_pid:
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


class TestRefusals:
    def test_잠금_경로가_링크이면_LockError이고_대상은_그대로다(self, tmp_dir: Path) -> None:
        target = tmp_dir / "target.txt"
        target.write_text("원본", encoding="utf-8")
        link = tmp_dir / "sync.lock"
        link.symlink_to(target)
        with pytest.raises(LockError):
            acquire_lock(link, now=NOW)
        assert target.read_text(encoding="utf-8") == "원본"
        assert link.is_symlink()

    def test_없는_대상을_가리키는_링크도_대상을_만들지_않는다(self, tmp_dir: Path) -> None:
        target = tmp_dir / "not-yet.txt"
        link = tmp_dir / "sync.lock"
        link.symlink_to(target)
        with pytest.raises(LockError):
            acquire_lock(link, now=NOW)
        assert not target.exists()

    def test_LockError_메시지에_경로나_내용이_없다(self, tmp_dir: Path) -> None:
        link = tmp_dir / "비밀-경로-sync.lock"
        link.symlink_to(tmp_dir / "대상")
        with pytest.raises(LockError) as info:
            acquire_lock(link, now=NOW)
        assert "비밀-경로" not in str(info.value)
        assert str(tmp_dir) not in str(info.value)

    def test_폴더를_만들_수_없으면_LockError이다(self, tmp_dir: Path) -> None:
        blocker = tmp_dir / "file"
        blocker.write_text("x", encoding="utf-8")
        with pytest.raises(LockError) as info:
            acquire_lock(blocker / "state" / "sync.lock", now=NOW)
        assert str(tmp_dir) not in str(info.value)

    def test_잠금_경로가_폴더이면_LockError이다(self, tmp_dir: Path) -> None:
        directory = tmp_dir / "sync.lock"
        directory.mkdir()
        with pytest.raises(LockError):
            acquire_lock(directory, now=NOW)

    def test_잠금_경로가_FIFO이면_멈추지_않고_LockError이다(self, tmp_dir: Path) -> None:
        fifo = tmp_dir / "sync.lock"
        os.mkfifo(fifo)
        with pytest.raises(LockError):
            acquire_lock(fifo, now=NOW)

    def test_경로에_NUL이_있어도_LockError이다(self, tmp_dir: Path) -> None:
        with pytest.raises(LockError):
            acquire_lock(Path(str(tmp_dir / "a\0b")), now=NOW)

    def test_읽기_전용_폴더에서_파일을_만들_수_없으면_LockError이다(self, tmp_dir: Path) -> None:
        if os.geteuid() == 0:
            pytest.skip("root는 권한 검사를 건너뛴다")
        directory = tmp_dir / "ro"
        directory.mkdir()
        directory.chmod(0o500)
        try:
            with pytest.raises(LockError):
                acquire_lock(directory / "sync.lock", now=NOW)
        finally:
            directory.chmod(0o700)


class TestReadOwner:
    def test_pid와_시작_시각을_읽는다(self, tmp_dir: Path) -> None:
        file = tmp_dir / "sync.lock"
        file.write_text("pid=1234 started=2026-10-07T09:00:00+09:00\n", encoding="utf-8")
        assert read_lock_owner(file) == LockOwner(1234, datetime(2026, 10, 7, 0, 0, tzinfo=UTC))

    def test_잡은_잠금의_내용을_그대로_읽는다(self, tmp_dir: Path) -> None:
        file = tmp_dir / "sync.lock"
        lock = acquire_lock(file, now=NOW)
        assert lock is not None
        try:
            assert read_lock_owner(file) == LockOwner(os.getpid(), NOW)
        finally:
            lock.release()

    def test_없는_파일_빈_파일_쓰레기는_빈_필드다(self, tmp_dir: Path) -> None:
        empty = LockOwner(None, None)
        assert read_lock_owner(tmp_dir / "missing.lock") == empty
        blank = tmp_dir / "blank.lock"
        blank.write_bytes(b"")
        assert read_lock_owner(blank) == empty
        junk = tmp_dir / "junk.lock"
        junk.write_bytes(b"\xff\xfe\x00 garbage \x00\n\x80")
        assert read_lock_owner(junk) == empty
        words = tmp_dir / "words.lock"
        words.write_text("쓰레기 내용", encoding="utf-8")
        assert read_lock_owner(words) == empty

    @pytest.mark.parametrize(
        "pid_text",
        ["0", "-5", "+5", "1.5", "1e3", "٣٤٥", "99999999999999999999", "", "0x10"],
    )
    def test_pid가_이상하면_pid만_None이다(self, tmp_dir: Path, pid_text: str) -> None:
        file = tmp_dir / "sync.lock"
        file.write_text(f"pid={pid_text} started=2026-10-07T09:00:00+09:00\n", encoding="utf-8")
        owner = read_lock_owner(file)
        assert owner.pid is None
        assert owner.started_at == datetime(2026, 10, 7, 0, 0, tzinfo=UTC)

    @pytest.mark.parametrize("started", ["어제", "2026-13-45T99:00:00", "9" * 80, "", "\x1b[31m"])
    def test_시각이_이상하면_시각만_None이다(self, tmp_dir: Path, started: str) -> None:
        file = tmp_dir / "sync.lock"
        file.write_text(f"pid=77 started={started}\n", encoding="utf-8")
        owner = read_lock_owner(file)
        assert owner.pid == 77
        assert owner.started_at is None

    def test_앞_4KB만_읽는다(self, tmp_dir: Path) -> None:
        file = tmp_dir / "sync.lock"
        file.write_bytes(b"#" * 5000 + b"\npid=1234 started=2026-10-07T09:00:00+09:00\n")
        assert read_lock_owner(file) == LockOwner(None, None)

    def test_링크는_따라가지_않는다(self, tmp_dir: Path) -> None:
        target = tmp_dir / "target.txt"
        target.write_text("pid=1234 started=2026-10-07T09:00:00+09:00\n", encoding="utf-8")
        link = tmp_dir / "sync.lock"
        link.symlink_to(target)
        assert read_lock_owner(link) == LockOwner(None, None)

    def test_FIFO와_폴더에서_멈추거나_던지지_않는다(self, tmp_dir: Path) -> None:
        fifo = tmp_dir / "fifo.lock"
        os.mkfifo(fifo)
        assert read_lock_owner(fifo) == LockOwner(None, None)
        directory = tmp_dir / "dir.lock"
        directory.mkdir()
        assert read_lock_owner(directory) == LockOwner(None, None)

    def test_NUL이_든_경로도_던지지_않는다(self, tmp_dir: Path) -> None:
        assert read_lock_owner(Path(str(tmp_dir / "a\0b"))) == LockOwner(None, None)

    def test_첫_줄의_값이_이긴다(self, tmp_dir: Path) -> None:
        file = tmp_dir / "sync.lock"
        file.write_text(
            "pid=11 started=2026-10-07T09:00:00+09:00\npid=22 started=2000-01-01T00:00:00Z\n",
            encoding="utf-8",
        )
        assert read_lock_owner(file).pid == 11


class TestHeldTooLong:
    def test_7시간_전에_시작했으면_너무_오래다(self) -> None:
        owner = LockOwner(1, NOW - timedelta(hours=7))
        assert held_too_long(owner, NOW)

    def test_정확히_6시간은_아니고_1초_넘으면_그렇다(self) -> None:
        assert LONG_HELD == timedelta(hours=6)
        assert not held_too_long(LockOwner(1, NOW - LONG_HELD), NOW)
        assert held_too_long(LockOwner(1, NOW - LONG_HELD - timedelta(seconds=1)), NOW)

    def test_시작_시각을_모르면_아니다(self) -> None:
        assert not held_too_long(LockOwner(1, None), NOW)
        assert not held_too_long(LockOwner(None, None), NOW)

    def test_미래_시각이나_naive_now도_던지지_않는다(self) -> None:
        assert not held_too_long(LockOwner(1, NOW + timedelta(hours=9)), NOW)
        naive_now = datetime(2026, 10, 7, 0, 0, 0)
        assert held_too_long(LockOwner(1, NOW - timedelta(hours=7)), naive_now)

    def test_파일에서_읽은_값으로_판정한다(self, tmp_dir: Path) -> None:
        file = tmp_dir / "sync.lock"
        started = NOW - timedelta(hours=7)
        lock = acquire_lock(file, now=started)
        assert lock is not None
        try:
            assert held_too_long(read_lock_owner(file), NOW)
        finally:
            lock.release()


_FORK_HOLDER = textwrap.dedent(
    """
    import os, sys
    from datetime import UTC, datetime
    from pathlib import Path
    from team_wiki_engine.sync.lock import acquire_lock
    path = Path(sys.argv[1])
    lock = acquire_lock(path, now=datetime.now(UTC))
    assert lock is not None
    child = os.fork()
    if child == 0:  # exec 없이 fork한 자식이 잠금 객체를 정리한다(release와 소멸자 모두)
        lock.release()
        del lock
        os._exit(0)
    os.waitpid(child, 0)
    again = acquire_lock(path, now=datetime.now(UTC))
    content = path.read_text().startswith("pid=%d " % os.getpid())
    print("held=%s second=%s content=%s" % (lock.held, again, content), flush=True)
    """
)


class TestHardening:
    def test_하드링크된_잠금_파일은_내용을_건드리기_전에_거부한다(self, tmp_dir: Path) -> None:
        victim = tmp_dir / "victim.txt"
        victim.write_text("소중한 내용", encoding="utf-8")
        victim.chmod(0o644)
        file = tmp_dir / "sync.lock"
        os.link(victim, file)
        with pytest.raises(LockError):
            acquire_lock(file, now=NOW)
        assert victim.read_text(encoding="utf-8") == "소중한 내용"
        assert stat.S_IMODE(victim.stat().st_mode) == 0o644

    @pytest.mark.parametrize("device", ["/dev/null", "/dev/zero"])
    def test_장치_파일은_바로_LockError이다(self, device: str) -> None:
        started = time.monotonic()
        with pytest.raises(LockError):
            acquire_lock(Path(device), now=NOW)
        assert time.monotonic() - started < 2

    async def test_fork만_한_자식의_release는_부모의_잠금을_풀지_않는다(
        self, tmp_dir: Path
    ) -> None:
        proc = await _spawn(_FORK_HOLDER, tmp_dir / "sync.lock")
        try:
            assert await _first_line(proc) == "held=True second=None content=True"
        finally:
            await _kill(proc)

    def test_시작_시각이_naive여도_너무_오래_판정은_던지지_않는다(self) -> None:
        naive = datetime(2026, 10, 6, 0, 0, 0)
        assert held_too_long(LockOwner(1, naive), NOW)  # naive는 UTC로 본다(now와 같은 규칙)
        assert not held_too_long(LockOwner(1, naive), datetime(2026, 10, 6, 1, 0, 0))

    def test_4KB_뒤의_started는_읽지_않는다(self, tmp_dir: Path) -> None:
        file = tmp_dir / "sync.lock"
        file.write_text(
            "pid=1 " + " " * 5000 + "started=2026-10-07T09:00:00+09:00\n", encoding="utf-8"
        )
        owner = read_lock_owner(file)
        assert owner.pid == 1
        assert owner.started_at is None

    def test_실패한_시도는_fd를_새지_않는다(self, tmp_dir: Path) -> None:
        file = tmp_dir / "sync.lock"
        link = tmp_dir / "link.lock"
        link.symlink_to(tmp_dir / "target")
        held = acquire_lock(file, now=NOW)
        assert held is not None
        try:
            before = len(os.listdir("/dev/fd"))
            for _ in range(100):
                assert acquire_lock(file, now=NOW) is None
                with pytest.raises(LockError):
                    acquire_lock(link, now=NOW)
                with pytest.raises(LockError):
                    acquire_lock(Path("/dev/null"), now=NOW)
            assert len(os.listdir("/dev/fd")) == before
        finally:
            held.release()

    def test_놓지_않고_버려진_잠금은_소멸자가_푼다(self, tmp_dir: Path) -> None:
        file = tmp_dir / "sync.lock"
        lock = acquire_lock(file, now=NOW)
        assert lock is not None
        del lock
        gc.collect()
        again = acquire_lock(file, now=NOW)
        assert again is not None
        again.release()
