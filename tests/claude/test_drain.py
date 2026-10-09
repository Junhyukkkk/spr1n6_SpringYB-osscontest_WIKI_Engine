"""`drain_background_tasks`: 버려진 Claude 스트림의 정리를 기다리는 함수."""

import asyncio
import gc
import logging
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import ClaudeAgentOptions

from team_wiki_engine.claude import runner as runner_module
from team_wiki_engine.claude.prompt import clear_prompt_cache
from team_wiki_engine.claude.runner import RunnerDeps, create_claude_runner, drain_background_tasks
from team_wiki_engine.claude.types import RunClaudeInput
from tests.helpers import write_tree

SKILLS = {
    f"skills/{name}/SKILL.md": f"---\nname: {name}\ndescription: d\n---\n본문"
    for name in ("wiki-schema", "ask", "sync-code", "ingest")
}


@pytest.fixture(autouse=True)
def _isolated_background_set(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """다른 테스트가 남긴 태스크와 섞이지 않게 빈 집합으로 시작한다."""
    clear_prompt_cache()
    monkeypatch.setattr(runner_module, "_background_tasks", set())
    yield
    clear_prompt_cache()


class SwallowingQuery:
    """멈춰 있다가 취소를 받으면 삼키고 0.3초 더 버티는 가짜 스트림."""

    def __init__(self) -> None:
        self.finished = False

    def __call__(self, *, prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.3)
        self.finished = True
        return
        yield  # pragma: no cover - 비동기 제너레이터로 만들기 위한 문장


def _input(root: Path) -> RunClaudeInput:
    return RunClaudeInput(
        profile="bot",
        prompt="질문",
        cwd=root / "data",
        read_roots=(),
        write_roots=(),
        timeout_ms=30,
    )


async def test_returns_zero_immediately_when_nothing_to_drain() -> None:
    started = time.monotonic()
    async with asyncio.timeout(10):
        assert await drain_background_tasks(5) == 0
    assert time.monotonic() - started < 0.5


async def test_waits_for_a_runner_cleanup_that_outlived_the_result(tmp_dir: Path) -> None:
    write_tree(tmp_dir, {"data/wiki/index.md": "# 목차", **SKILLS})
    fake = SwallowingQuery()
    deps = RunnerDeps(
        model="m",
        effort={"bot": "low", "sync": "low"},
        skills_dir=tmp_dir / "skills",
        env={},
        query_fn=fake,
        hard_deadline_grace_ms=50,
    )
    async with asyncio.timeout(10):
        result = await create_claude_runner(deps)(_input(tmp_dir))
        assert (result.ok, result.failure) == (False, "timeout")
        assert fake.finished is False  # 결과가 정리보다 먼저 돌아왔다
        assert len(runner_module._background_tasks) == 1
        assert await drain_background_tasks(5) == 0
    assert fake.finished is True
    assert runner_module._background_tasks == set()


async def test_returns_remaining_count_without_cancelling_unfinished_tasks(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    release = asyncio.Event()

    async def wait_forever() -> None:
        await release.wait()

    task = asyncio.create_task(wait_forever())
    runner_module._background_tasks.add(task)
    try:
        started = time.monotonic()
        with caplog.at_level("WARNING", logger=runner_module.logger.name):
            async with asyncio.timeout(10):
                assert await drain_background_tasks(0.05) == 1
        assert time.monotonic() - started < 0.5
        assert not task.done()
        assert not task.cancelling()
        assert task in runner_module._background_tasks
        warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1
        assert "1" in warnings[0]
        assert "Task" not in warnings[0]  # 태스크 repr을 싣지 않는다
    finally:
        release.set()
        await task
        runner_module._background_tasks.discard(task)


async def test_finished_tasks_are_dropped_and_not_counted() -> None:
    async def quick() -> None:
        return None

    task = asyncio.create_task(quick())
    await task
    runner_module._background_tasks.add(task)
    assert await drain_background_tasks(1) == 0
    assert runner_module._background_tasks == set()


async def test_failed_task_does_not_raise() -> None:
    async def boom() -> None:
        raise RuntimeError("boom")

    task = asyncio.create_task(boom())
    runner_module._background_tasks.add(task)
    task.add_done_callback(runner_module._on_background_done)
    async with asyncio.timeout(10):
        assert await drain_background_tasks(1) == 0
    assert runner_module._background_tasks == set()


def test_task_of_a_closed_loop_is_dropped_without_error() -> None:
    other = asyncio.new_event_loop()
    stuck: asyncio.Task[None] | None = None
    try:

        async def park() -> None:
            await asyncio.Event().wait()

        stuck = other.create_task(park())
        other.run_until_complete(asyncio.sleep(0))  # 태스크가 시작돼 대기 중
    finally:
        # 닫힌 루프에서 영영 끝나지 않을 태스크가 GC될 때 나는 로그를 막는다.
        other.set_exception_handler(lambda _loop, _ctx: None)
        other.close()
    assert stuck is not None and not stuck.done()
    runner_module._background_tasks.add(stuck)

    async def main() -> int:
        async with asyncio.timeout(10):
            return await drain_background_tasks(0.05)

    assert asyncio.run(main()) == 0
    assert runner_module._background_tasks == set()


async def test_outer_cancel_propagates_and_leaves_the_task_alone() -> None:
    release = asyncio.Event()

    async def wait_forever() -> None:
        await release.wait()

    inner = asyncio.create_task(wait_forever())
    runner_module._background_tasks.add(inner)
    waiter = asyncio.create_task(drain_background_tasks(30))
    try:
        async with asyncio.timeout(10):
            await asyncio.sleep(0.05)  # drain이 기다리는 중
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
        assert not inner.done()
        assert not inner.cancelling()
    finally:
        release.set()
        await inner
        runner_module._background_tasks.discard(inner)


async def test_negative_timeout_is_treated_as_zero() -> None:
    release = asyncio.Event()

    async def wait_forever() -> None:
        await release.wait()

    task = asyncio.create_task(wait_forever())
    runner_module._background_tasks.add(task)
    try:
        started = time.monotonic()
        async with asyncio.timeout(10):
            assert await drain_background_tasks(-5) == 1
        assert time.monotonic() - started < 0.5
        assert not task.done()
    finally:
        release.set()
        await task
        runner_module._background_tasks.discard(task)


async def test_nan_timeout_returns_promptly_without_hanging() -> None:
    release = asyncio.Event()

    async def wait_forever() -> None:
        await release.wait()

    task = asyncio.create_task(wait_forever())
    runner_module._background_tasks.add(task)
    try:
        started = time.monotonic()
        async with asyncio.timeout(5):
            assert await drain_background_tasks(float("nan")) == 1
        assert time.monotonic() - started < 0.5
    finally:
        release.set()
        await task
        runner_module._background_tasks.discard(task)


def test_task_of_another_open_loop_is_neither_waited_on_nor_counted() -> None:
    other = asyncio.new_event_loop()

    async def park() -> None:
        await asyncio.Event().wait()

    foreign = other.create_task(park())
    other.run_until_complete(asyncio.sleep(0))
    runner_module._background_tasks.add(foreign)
    try:

        async def main() -> tuple[int, float]:
            started = time.monotonic()
            async with asyncio.timeout(5):
                count = await drain_background_tasks(2)
            return count, time.monotonic() - started

        count, elapsed = asyncio.run(main())
        assert count == 0
        assert elapsed < 1  # 다른 루프의 태스크를 기다리지 않았다
        assert foreign in runner_module._background_tasks  # 열린 루프의 태스크는 건드리지 않는다
        assert not foreign.done()
        assert not foreign.cancelling()
    finally:
        runner_module._background_tasks.discard(foreign)
        foreign.cancel()
        other.run_until_complete(asyncio.sleep(0))
        other.close()


async def test_task_spawned_during_the_wait_is_awaited_too() -> None:
    order: list[str] = []
    second: list[asyncio.Task[None]] = []

    async def child() -> None:
        await asyncio.sleep(0.15)
        order.append("child")

    async def parent() -> None:
        await asyncio.sleep(0.05)
        task = asyncio.create_task(child())
        second.append(task)
        runner_module._background_tasks.add(task)
        order.append("parent")

    first = asyncio.create_task(parent())
    runner_module._background_tasks.add(first)
    async with asyncio.timeout(10):
        assert await drain_background_tasks(5) == 0
    assert order == ["parent", "child"]
    assert second[0].done()


async def test_task_spawned_during_the_wait_is_counted_when_the_deadline_passes() -> None:
    release = asyncio.Event()
    second: list[asyncio.Task[None]] = []

    async def child() -> None:
        await release.wait()

    async def parent() -> None:
        await asyncio.sleep(0.05)
        task = asyncio.create_task(child())
        second.append(task)
        runner_module._background_tasks.add(task)

    first = asyncio.create_task(parent())
    runner_module._background_tasks.add(first)
    try:
        started = time.monotonic()
        async with asyncio.timeout(10):
            assert await drain_background_tasks(0.3) == 1
        assert time.monotonic() - started < 1.5
        assert not second[0].done()
        assert not second[0].cancelling()
    finally:
        release.set()
        await asyncio.gather(first, *second)
        runner_module._background_tasks.clear()


async def test_pruning_a_finished_failed_task_retrieves_its_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "sk-ant-SECRET-VALUE"

    async def boom() -> None:
        raise RuntimeError(secret)

    task = asyncio.create_task(boom())
    await asyncio.wait({task})
    runner_module._background_tasks.add(task)  # 완료 콜백 없이 직접 넣는다
    with caplog.at_level(logging.DEBUG):
        assert await drain_background_tasks(1) == 0
        del task
        gc.collect()
    assert runner_module._background_tasks == set()
    assert "never retrieved" not in caplog.text
    assert secret not in caplog.text
