"""사용량 제한: 사용자별 동시 실행·분당 한도, 하루 총량(KST), 전체 동시 실행 수.

분당 창은 단조 시계만 쓴다(벽시계가 뒤로 가도 창이 늘지 않는다). 날짜만 벽시계(KST)다.
거절은 어느 카운터도 올리지 않는다. 줄을 세우지 않는다.
"""

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

from team_wiki_engine.config.schema import LimitsConfig
from team_wiki_engine.util.time import kst_parts

RejectReason = Literal["user_busy", "user_rate", "daily_quota", "busy"]

WINDOW_S: Final = 60.0
MAX_TRACKED_USERS: Final = 10_000
_PRUNE_EVERY: Final = 256


class Slot:
    """진행 중 자리. `release()`는 여러 번 불러도 한 번만 줄인다."""

    def __init__(self, release: Callable[[], None]) -> None:
        self._release: Callable[[], None] | None = release

    def release(self) -> None:
        release, self._release = self._release, None
        if release is not None:
            release()


@dataclass(frozen=True)
class Accepted:
    slot: Slot
    ok: Literal[True] = True


@dataclass(frozen=True)
class Rejected:
    reason: RejectReason
    ok: Literal[False] = False


Acquire = Accepted | Rejected


class Limiter:
    def __init__(
        self,
        *,
        per_user_per_minute: int,
        daily_total: int,
        concurrent: int,
        per_user_concurrent: int = 1,
        now: Callable[[], datetime],
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        for value in (per_user_per_minute, daily_total, concurrent, per_user_concurrent):
            if value < 1:
                raise ValueError("사용량 제한 값은 1 이상이어야 합니다")
        self._per_minute = per_user_per_minute
        self._daily_total = daily_total
        self._concurrent = concurrent
        self._per_user_concurrent = per_user_concurrent
        self._now = now
        self._monotonic = monotonic
        self._times: dict[str, deque[float]] = {}
        self._active_users: dict[str, int] = {}
        self._active = 0
        self._date = kst_parts(now()).date
        self._day_count = 0
        self._calls = 0

    @classmethod
    def from_limits(
        cls,
        limits: LimitsConfig,
        *,
        now: Callable[[], datetime],
        monotonic: Callable[[], float] = time.monotonic,
    ) -> "Limiter":
        return cls(
            per_user_per_minute=limits.per_user_per_minute,
            daily_total=limits.daily_total,
            concurrent=limits.concurrent,
            per_user_concurrent=1,
            now=now,
            monotonic=monotonic,
        )

    @property
    def daily_total(self) -> int:
        return self._daily_total

    # ------------------------------------------------------------ 조회
    def _roll(self) -> None:
        today = kst_parts(self._now()).date
        if today > self._date:  # 날짜 문자열은 YYYY-MM-DD라 사전순이 시간순이다; 뒤로 가도 안 센다
            self._date = today
            self._day_count = 0

    def active(self) -> int:
        return self._active

    def active_for(self, user_id: str) -> int:
        return self._active_users.get(user_id, 0)

    def daily_used(self) -> int:
        self._roll()
        return self._day_count

    def seed_daily(self, count: int) -> None:
        self._roll()
        self._day_count = max(count, 0)

    def tracked_users(self) -> int:
        return len(self._times.keys() | self._active_users.keys())

    # ------------------------------------------------------------ 판정
    def _recent(self, user_id: str, now_m: float) -> deque[float]:
        times = self._times.get(user_id)
        if times is None:
            return deque()
        while times and now_m - times[0] >= WINDOW_S:
            times.popleft()
        return times

    def _prune(self, now_m: float) -> None:
        for user in list(self._times):
            if not self._recent(user, now_m):
                del self._times[user]
        for user in [u for u, n in self._active_users.items() if n <= 0]:
            del self._active_users[user]

    def acquire(self, user_id: str) -> Acquire:
        now_m = self._monotonic()
        self._roll()
        self._calls += 1
        if self._calls % _PRUNE_EVERY == 0 or (
            self.tracked_users() > MAX_TRACKED_USERS and self._calls % 16 == 0
        ):
            self._prune(now_m)
        if self._active_users.get(user_id, 0) >= self._per_user_concurrent:
            return Rejected("user_busy")
        recent = self._recent(user_id, now_m)
        if len(recent) >= self._per_minute:
            return Rejected("user_rate")
        if self._day_count >= self._daily_total:
            return Rejected("daily_quota")
        if self._active >= self._concurrent:
            return Rejected("busy")
        self._times.setdefault(user_id, recent).append(now_m)
        self._day_count += 1
        self._active += 1
        self._active_users[user_id] = self._active_users.get(user_id, 0) + 1
        return Accepted(Slot(lambda: self._release(user_id)))

    def _release(self, user_id: str) -> None:
        self._active = max(self._active - 1, 0)
        left = self._active_users.get(user_id, 0) - 1
        if left > 0:
            self._active_users[user_id] = left
        else:
            self._active_users.pop(user_id, None)
