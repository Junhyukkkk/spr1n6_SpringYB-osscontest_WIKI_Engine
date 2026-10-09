from datetime import UTC, datetime

from tests.helpers.clock import ManualClock


def test_manual_clock_advances_both() -> None:
    clock = ManualClock(datetime(2026, 10, 7, tzinfo=UTC))
    assert clock.now().tzinfo is not None
    wall, mono = clock.now(), clock.monotonic()
    clock.advance(30)
    assert (clock.now() - wall).total_seconds() == 30
    assert clock.monotonic() - mono == 30
    clock.set_wall(wall)
    assert clock.now() == wall
    assert clock.monotonic() - mono == 30
