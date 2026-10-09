"""손으로 움직이는 가짜 시계: 벽시계(aware)와 단조 시계를 따로 둔다."""

from datetime import datetime, timedelta


class ManualClock:
    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("시작 시각은 시간대가 있어야 합니다")
        self._wall = start
        self._mono = 1000.0

    def now(self) -> datetime:
        return self._wall

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        """벽시계와 단조 시계를 함께 앞으로 보낸다."""
        self._wall += timedelta(seconds=seconds)
        self._mono += seconds

    def set_wall(self, d: datetime) -> None:
        """벽시계만 바꾼다(뒤로도 갈 수 있다)."""
        if d.tzinfo is None:
            raise ValueError("시각은 시간대가 있어야 합니다")
        self._wall = d
