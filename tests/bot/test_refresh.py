import asyncio
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from team_wiki_engine.bot.data_gate import DataGate
from team_wiki_engine.bot.refresh import (
    NOTIFY_EVERY_FAILURES,
    DataRefresher,
    data_ready,
)
from team_wiki_engine.git.client import GitClient, GitError
from team_wiki_engine.sync.data_copy import prepare_data_copy
from team_wiki_engine.sync.stage import Mask
from tests.helpers.clock import ManualClock
from tests.helpers.git import commit_files, seed_bare
from tests.helpers.notify import RecordingNotifier

NOW = datetime(2026, 10, 7, tzinfo=UTC)


def make(
    tmp_path: Path,
    refresh: Callable[[], Awaitable[None]],
    *,
    mask: Mask | None = None,
    interval_s: float = 600.0,
    not_ready_interval_s: float = 60.0,
    timeout_s: float = 180.0,
) -> tuple[DataRefresher, DataGate, RecordingNotifier]:
    gate = DataGate()
    notifier = RecordingNotifier()
    clock = ManualClock(NOW)
    refresher = DataRefresher(
        refresh=refresh,
        gate=gate,
        data_dir=tmp_path / "data",
        notifier=notifier,
        mask=mask or Mask((), {}),
        now=clock.now,
        interval_s=interval_s,
        not_ready_interval_s=not_ready_interval_s,
        timeout_s=timeout_s,
    )
    return refresher, gate, notifier


async def test_success_runs_inside_write_gate(tmp_path: Path) -> None:
    async with asyncio.timeout(10):
        seen: list[bool] = []

        async def refresh() -> None:
            seen.append(gate.writer_active)

        refresher, gate, notifier = make(tmp_path, refresh)
        assert await refresher.refresh_once() is True
        assert seen == [True]
        assert not gate.writer_active
        assert refresher.state.last_ok_at == NOW
        assert refresher.state.consecutive_failures == 0
        assert notifier.sent == []


async def test_first_failure_notifies_masked(tmp_path: Path) -> None:
    async with asyncio.timeout(10):

        async def refresh() -> None:
            raise GitError("fail ghp_secret_value_123 at /srv/x/y/z", kind="exit")

        mask = Mask(("ghp_secret_value_123",), {"/srv/x": "<root>"})
        refresher, _, notifier = make(tmp_path, refresh, mask=mask)
        assert await refresher.refresh_once() is False
        assert len(notifier.sent) == 1
        text, mention = notifier.sent[0]
        assert mention is True
        assert "ghp_secret" not in text and "/srv/x" not in text
        assert "연속 1회" in text


async def test_notify_on_first_and_every_sixth_then_recover(tmp_path: Path) -> None:
    async with asyncio.timeout(10):
        fail = True

        async def refresh() -> None:
            if fail:
                raise RuntimeError("비밀")

        refresher, _, notifier = make(tmp_path, refresh)
        for _ in range(NOTIFY_EVERY_FAILURES):
            await refresher.refresh_once()
        assert [("연속 1회" in t, "연속 6회" in t) for t, _ in notifier.sent] == [
            (True, False),
            (False, True),
        ]
        assert all(m for _, m in notifier.sent)
        fail = False
        assert await refresher.refresh_once() is True
        assert notifier.sent[-1] == ("[bot] data 사본 갱신 회복", False)
        assert len(notifier.sent) == 3
        assert refresher.state.consecutive_failures == 0
        assert refresher.state.last_error is None


async def test_unknown_exception_only_type_name(tmp_path: Path) -> None:
    async with asyncio.timeout(10):

        async def refresh() -> None:
            raise RuntimeError("비밀")

        refresher, _, notifier = make(tmp_path, refresh)
        await refresher.refresh_once()
        assert refresher.state.last_error == "RuntimeError"
        assert "비밀" not in notifier.sent[0][0]


async def test_hang_times_out(tmp_path: Path) -> None:
    async with asyncio.timeout(10):

        async def refresh() -> None:
            await asyncio.Event().wait()

        refresher, gate, notifier = make(tmp_path, refresh, timeout_s=0.1)
        assert await refresher.refresh_once() is False
        assert refresher.state.last_error == "시간 초과"
        assert "시간 초과" in notifier.sent[0][0]
        assert not gate.writer_active


async def test_oserror_reports_type_only(tmp_path: Path) -> None:
    async with asyncio.timeout(10):

        async def refresh() -> None:
            raise PermissionError("/srv/secret/path")

        refresher, _, _ = make(tmp_path, refresh)
        await refresher.refresh_once()
        assert refresher.state.last_error == "PermissionError"


async def test_outer_cancel_propagates_and_clears_gate(tmp_path: Path) -> None:
    async with asyncio.timeout(10):
        started = asyncio.Event()

        async def refresh() -> None:
            started.set()
            await asyncio.Event().wait()

        refresher, gate, notifier = make(tmp_path, refresh)
        task = asyncio.create_task(refresher.refresh_once())
        await started.wait()
        assert gate.writer_active
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not gate.writer_active
        assert notifier.sent == []


async def test_notice_is_sent_after_leaving_gate(tmp_path: Path) -> None:
    async with asyncio.timeout(10):

        async def refresh() -> None:
            raise RuntimeError

        refresher, gate, _ = make(tmp_path, refresh)
        active: list[bool] = []

        class Spy:
            async def send(self, text: str, *, mention_admin: bool = False) -> None:
                active.append(gate.writer_active)

        refresher._notifier = Spy()
        await refresher.refresh_once()
        assert active == [False]


async def test_run_forever_intervals(tmp_path: Path) -> None:
    async with asyncio.timeout(10):
        calls = 0
        done = asyncio.Event()

        async def refresh() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:  # 첫 갱신이 사본을 만든다 → 이후 긴 간격
                (tmp_path / "data" / ".git").mkdir(parents=True)
                (tmp_path / "data" / "wiki").mkdir()
            if calls == 3:
                done.set()

        refresher, _, _ = make(tmp_path, refresh, interval_s=0.3, not_ready_interval_s=0.01)
        loop = asyncio.get_running_loop()
        start = loop.time()
        task = asyncio.create_task(refresher.run_forever())
        await done.wait()
        elapsed = loop.time() - start
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # 1회째는 짧은 간격(0.01), 2·3회째는 긴 간격(0.3)
        assert 0.6 <= elapsed < 5


def test_data_ready(tmp_path: Path) -> None:
    data = tmp_path / "data"
    assert not data_ready(data)
    (data / ".git").mkdir(parents=True)
    assert not data_ready(data)
    (data / "wiki").mkdir()
    assert data_ready(data)
    (data / "wiki").rmdir()
    (tmp_path / "w").mkdir()
    os.symlink(tmp_path / "w", data / "wiki")
    assert not data_ready(data)
    os.unlink(data / "wiki")
    (data / "wiki").mkdir()
    (data / ".git").rmdir()
    (data / ".git").write_text("gitdir: x")
    assert not data_ready(data)


async def test_real_git_refresh_and_reader_blocks_update(tmp_path: Path) -> None:
    async with asyncio.timeout(60):
        bare = tmp_path / "bare.git"
        work = await seed_bare(bare, tmp_path / "work", {"wiki/a.md": "v1\n"})
        data_dir = tmp_path / "data"
        git = GitClient(data_dir, allowed_protocols="file")

        async def refresh() -> None:
            await prepare_data_copy(git, str(bare), "main")

        refresher, gate, _ = make(tmp_path, refresh)
        assert await refresher.refresh_once() is True
        assert data_ready(data_dir)
        assert (data_dir / "wiki/a.md").read_text() == "v1\n"

        await commit_files(work, {"wiki/b.md": "new\n"}, "more")
        await work.run(["push", "-q", "origin", "main"])
        await commit_files(work, {"wiki/a.md": "v2\n"}, "edit")
        await work.run(["push", "-q", "origin", "main"])

        async with gate.reading(timeout_s=1) as ok:
            assert ok
            task = asyncio.create_task(refresher.refresh_once())
            first = (data_dir / "wiki/a.md").read_text()
            await asyncio.sleep(0.3)
            second = (data_dir / "wiki/a.md").read_text()
            assert first == second == "v1\n"
            assert not task.done()
        assert await task is True
        assert (data_dir / "wiki/b.md").read_text() == "new\n"
        assert (data_dir / "wiki/a.md").read_text() == "v2\n"
