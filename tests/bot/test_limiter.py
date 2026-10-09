from datetime import UTC, datetime

import pytest

from team_wiki_engine.bot.limiter import (
    MAX_TRACKED_USERS,
    WINDOW_S,
    Accepted,
    Limiter,
    Rejected,
)
from team_wiki_engine.config.schema import LimitsConfig
from tests.helpers.clock import ManualClock

START = datetime(2026, 10, 7, 1, 0, tzinfo=UTC)


def make(
    clock: ManualClock,
    *,
    per_minute: int = 2,
    daily: int = 60,
    concurrent: int = 2,
    per_user_concurrent: int = 10,
) -> Limiter:
    return Limiter(
        per_user_per_minute=per_minute,
        daily_total=daily,
        concurrent=concurrent,
        per_user_concurrent=per_user_concurrent,
        now=clock.now,
        monotonic=clock.monotonic,
    )


def take(limiter: Limiter, user: str) -> Accepted:
    result = limiter.acquire(user)
    assert isinstance(result, Accepted)
    return result


def test_per_user_per_minute() -> None:
    clock = ManualClock(START)
    lim = make(clock, concurrent=50)
    take(lim, "u1").slot.release()
    take(lim, "u1").slot.release()
    assert lim.acquire("u1") == Rejected("user_rate")
    take(lim, "u2").slot.release()  # 다른 사용자는 영향 없음
    clock.advance(WINDOW_S + 1)
    assert isinstance(lim.acquire("u1"), Accepted)


def test_daily_resets_at_kst_midnight() -> None:
    clock = ManualClock(datetime(2026, 10, 7, 13, 59, tzinfo=UTC))
    lim = make(clock, per_minute=100, daily=2, concurrent=50)
    take(lim, "a").slot.release()
    take(lim, "b").slot.release()
    assert lim.acquire("c") == Rejected("daily_quota")
    clock.set_wall(datetime(2026, 10, 7, 14, 0, tzinfo=UTC))  # 아직 KST 23:00
    assert lim.acquire("c") == Rejected("daily_quota")
    clock.set_wall(datetime(2026, 10, 7, 15, 0, tzinfo=UTC))  # KST 자정
    assert isinstance(lim.acquire("c"), Accepted)
    assert lim.daily_used() == 1


def test_busy_and_release_frees() -> None:
    clock = ManualClock(START)
    lim = make(clock, concurrent=1)
    first = take(lim, "a")
    assert lim.acquire("b") == Rejected("busy")
    first.slot.release()
    assert isinstance(lim.acquire("b"), Accepted)


def test_double_release_counts_once() -> None:
    clock = ManualClock(START)
    lim = make(clock, concurrent=2)
    a = take(lim, "a")
    take(lim, "b")
    a.slot.release()
    a.slot.release()
    assert lim.active() == 1
    assert lim.active_for("a") == 0
    assert isinstance(lim.acquire("c"), Accepted)
    assert lim.acquire("d") == Rejected("busy")


def test_rejection_bumps_no_counter() -> None:
    clock = ManualClock(START)
    lim = make(clock, per_minute=1, daily=10, concurrent=1)
    first = take(lim, "a")
    before = (lim.active(), lim.daily_used(), lim.active_for("a"), lim.active_for("b"))
    assert lim.acquire("a") == Rejected("user_rate")
    assert lim.acquire("b") == Rejected("busy")
    assert (lim.active(), lim.daily_used(), lim.active_for("a"), lim.active_for("b")) == before
    first.slot.release()
    # 거절된 시도가 분당 창에 쌓이지 않았다: 61초 뒤 바로 받는다
    clock.advance(WINDOW_S + 1)
    assert isinstance(lim.acquire("a"), Accepted)
    assert lim.daily_used() == 2


def test_user_busy_first_and_no_counters() -> None:
    clock = ManualClock(START)
    lim = Limiter(
        per_user_per_minute=1,
        daily_total=10,
        concurrent=5,
        now=clock.now,
        monotonic=clock.monotonic,
    )
    held = take(lim, "u")
    # user_rate도 걸리는 상황: user_busy가 먼저
    assert lim.acquire("u") == Rejected("user_busy")
    assert (lim.active(), lim.daily_used(), lim.active_for("u")) == (1, 1, 1)
    held.slot.release()
    assert lim.acquire("u") == Rejected("user_rate")
    clock.advance(WINDOW_S + 1)
    assert isinstance(lim.acquire("u"), Accepted)


def test_wall_clock_backwards_does_not_extend_window() -> None:
    clock = ManualClock(START)
    lim = make(clock, per_minute=1, concurrent=5)
    take(lim, "u").slot.release()
    clock.set_wall(START.replace(hour=0))  # 벽시계만 1시간 뒤로
    assert lim.acquire("u") == Rejected("user_rate")
    clock.advance(WINDOW_S + 1)
    clock.set_wall(START.replace(hour=0))
    assert isinstance(lim.acquire("u"), Accepted)


def test_wall_clock_next_day_resets_daily() -> None:
    clock = ManualClock(START)
    lim = make(clock, per_minute=100, concurrent=5)
    take(lim, "u").slot.release()
    assert lim.daily_used() == 1
    clock.set_wall(datetime(2026, 10, 9, 1, 0, tzinfo=UTC))
    assert lim.daily_used() == 0


def test_pruning_bounds_memory_and_keeps_active() -> None:
    clock = ManualClock(START)
    lim = make(clock, per_minute=5, daily=1_000_000, concurrent=1_000_000)
    for i in range(20_000):
        take(lim, f"u{i}").slot.release()
    held = take(lim, "holder")
    clock.advance(WINDOW_S + 1)
    for i in range(300):
        lim.acquire(f"n{i}")
    assert lim.tracked_users() <= MAX_TRACKED_USERS
    assert lim.active_for("holder") == 1
    held.slot.release()
    assert lim.active() >= 0


def test_seed_daily() -> None:
    clock = ManualClock(START)
    lim = make(clock, per_minute=100, daily=60, concurrent=5)
    lim.seed_daily(59)
    take(lim, "a").slot.release()
    assert lim.acquire("b") == Rejected("daily_quota")
    lim.seed_daily(-3)
    assert lim.daily_used() == 0


@pytest.mark.parametrize("field", ["per_user_per_minute", "daily_total", "concurrent"])
@pytest.mark.parametrize("bad", [0, -1])
def test_constructor_rejects_small(field: str, bad: int) -> None:
    clock = ManualClock(START)
    values = {"per_user_per_minute": 2, "daily_total": 60, "concurrent": 2, field: bad}
    with pytest.raises(ValueError) as info:
        Limiter(
            per_user_per_minute=values["per_user_per_minute"],
            daily_total=values["daily_total"],
            concurrent=values["concurrent"],
            now=clock.now,
        )
    assert str(bad) not in str(info.value)


def test_from_limits_defaults() -> None:
    clock = ManualClock(START)
    lim = Limiter.from_limits(LimitsConfig(), now=clock.now, monotonic=clock.monotonic)
    assert lim.daily_total == 60
    take(lim, "u")
    assert lim.acquire("u") == Rejected("user_busy")  # per_user_concurrent=1
    take(lim, "v")
    assert lim.acquire("w") == Rejected("busy")  # concurrent=2


def test_backwards_wall_clock_cannot_reset_daily() -> None:
    clock = ManualClock(START)
    lim = make(clock, per_minute=100, daily=2, concurrent=5)
    take(lim, "a").slot.release()
    take(lim, "b").slot.release()
    clock.set_wall(datetime(2026, 10, 6, 1, 0, tzinfo=UTC))  # 하루 전으로
    assert lim.daily_used() == 2
    assert lim.acquire("c") == Rejected("daily_quota")


def test_window_boundary_exactly_60s() -> None:
    clock = ManualClock(START)
    lim = make(clock, per_minute=1, concurrent=5)
    take(lim, "u").slot.release()
    clock.advance(WINDOW_S - 0.001)
    assert lim.acquire("u") == Rejected("user_rate")
    clock.advance(0.001)
    assert isinstance(lim.acquire("u"), Accepted)
