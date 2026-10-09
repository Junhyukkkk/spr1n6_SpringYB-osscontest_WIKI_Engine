import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from team_wiki_engine.bot import sessions as sessions_module
from team_wiki_engine.bot.sessions import (
    MAX_SESSIONS,
    SESSION_TTL,
    SessionEntry,
    SessionStore,
)
from team_wiki_engine.metrics.record import STATE_FILES
from team_wiki_engine.util.time import kst_iso
from tests.helpers.clock import ManualClock
from tests.helpers.hang_guard import hang_guard

START = datetime(2026, 10, 7, 1, 0, tzinfo=UTC)


def make(file: Path, clock: ManualClock, **kwargs: int) -> SessionStore:
    return SessionStore(file, now=clock.now, **kwargs)  # type: ignore[arg-type]


def entry(thread: str, session: str, at: datetime) -> dict[str, str]:
    return {"thread_id": thread, "session_id": session, "updated_at": kst_iso(at)}


def test_set_get_remove(tmp_dir: Path) -> None:
    file = tmp_dir / "sessions.json"
    store = make(file, ManualClock(START))
    assert store.get("111") is None
    store.set("111", "sess-1")
    assert store.get("111") == "sess-1"
    assert len(store) == 1
    data = json.loads(file.read_text(encoding="utf-8"))
    assert data == [entry("111", "sess-1", START)]
    store.remove("111")
    assert store.get("111") is None
    assert json.loads(file.read_text(encoding="utf-8")) == []


def test_remove_missing_does_not_write(tmp_dir: Path) -> None:
    file = tmp_dir / "sessions.json"
    make(file, ManualClock(START)).remove("111")
    assert not file.exists()


def test_survives_restart(tmp_dir: Path) -> None:
    file = tmp_dir / "sessions.json"
    clock = ManualClock(START)
    make(file, clock).set("111", "sess-1")
    clock.advance(3600)
    assert make(file, clock).get("111") == "sess-1"


def test_expires_after_ttl_and_file_cleaned(tmp_dir: Path) -> None:
    file = tmp_dir / "sessions.json"
    clock = ManualClock(START)
    store = make(file, clock)
    store.set("111", "sess-1")
    clock.advance(SESSION_TTL.total_seconds() - 1)
    assert store.get("111") == "sess-1"
    clock.advance(2)
    assert store.get("111") is None
    assert json.loads(file.read_text(encoding="utf-8")) == []


def test_corrupt_and_odd_entries(tmp_dir: Path) -> None:
    file = tmp_dir / "sessions.json"
    clock = ManualClock(START)
    file.write_text("{깨짐", encoding="utf-8")
    assert len(make(file, clock)) == 0
    good = entry("111", "sess-1", START)
    file.write_text(
        json.dumps([{"thread_id": "222", "session_id": "x"}, good, "문자열", 5, None]),
        encoding="utf-8",
    )
    store = make(file, clock)
    assert len(store) == 1
    assert store.get("111") == "sess-1"
    assert store.get("222") is None


def test_reading_never_rewrites_file(tmp_dir: Path) -> None:
    file = tmp_dir / "sessions.json"
    raw = json.dumps([entry("111", "a", START - timedelta(days=30)), "x"])
    file.write_text(raw, encoding="utf-8")
    store = make(file, ManualClock(START))
    assert len(store) == 0
    assert file.read_text(encoding="utf-8") == raw


@pytest.mark.parametrize(
    ("thread", "session"),
    [("t1", "ok"), ("1\n", "ok"), ("111", "-x"), ("111", "a" * 200), ("111", ""), ("111", "a b")],
)
def test_set_rejects_bad_ids(tmp_dir: Path, thread: str, session: str) -> None:
    file = tmp_dir / "sessions.json"
    store = make(file, ManualClock(START))
    with pytest.raises(ValueError) as info:
        store.set(thread, session)
    assert session not in str(info.value) or session == ""
    assert not file.exists()


def test_old_ts_dict_format_is_empty(tmp_dir: Path) -> None:
    file = tmp_dir / "sessions.json"
    file.write_text(json.dumps({"111": {"sessionId": "abc", "updatedAt": 1}}), encoding="utf-8")
    assert len(make(file, ManualClock(START))) == 0


def test_hostile_files_are_empty_and_fast(tmp_dir: Path) -> None:
    file = tmp_dir / "sessions.json"
    clock = ManualClock(START)
    hostile = [
        "a" * (1024 * 1024 + 10),
        "[" + ",".join(["{}"] * 400_000) + "]",
        "[" * 100_000,
    ]
    for text in hostile:
        file.write_text(text, encoding="utf-8")
        with hang_guard(2):
            assert len(make(file, clock)) == 0


def test_duplicates_future_and_garbage(tmp_dir: Path) -> None:
    file = tmp_dir / "sessions.json"
    clock = ManualClock(START)
    rows = [
        entry("111", "newer", START - timedelta(hours=1)),
        entry("111", "older", START - timedelta(hours=5)),
        entry("222", "future", START + timedelta(days=365)),
        {"thread_id": "333", "session_id": "bad", "updated_at": "쓰레기"},
    ]
    file.write_text(json.dumps(rows), encoding="utf-8")
    store = make(file, clock)
    assert len(store) == 1
    assert store.get("111") == "newer"


def test_max_entries_evicts_oldest(tmp_dir: Path) -> None:
    file = tmp_dir / "sessions.json"
    clock = ManualClock(START)
    store = make(file, clock, max_entries=3)
    for i in range(4):
        store.set(str(100 + i), f"s{i}")
        clock.advance(10)
    assert store.get("100") is None
    assert [store.get(str(100 + i)) for i in (1, 2, 3)] == ["s1", "s2", "s3"]
    assert len(json.loads(file.read_text(encoding="utf-8"))) == 3
    assert MAX_SESSIONS == 1000


def test_load_keeps_newest_when_over_max(tmp_dir: Path) -> None:
    file = tmp_dir / "sessions.json"
    rows = [entry(str(100 + i), f"s{i}", START - timedelta(minutes=10 - i)) for i in range(6)]
    file.write_text(json.dumps(rows), encoding="utf-8")
    store = make(file, ManualClock(START), max_entries=2)
    assert len(store) == 2
    assert store.get("105") == "s5" and store.get("104") == "s4"


def test_symlink_target_untouched(tmp_dir: Path) -> None:
    target = tmp_dir / "other.txt"
    target.write_text("원본", encoding="utf-8")
    file = tmp_dir / "sessions.json"
    os.symlink(target, file)
    make(file, ManualClock(START)).set("111", "sess-1")
    assert not file.is_symlink()
    assert target.read_text(encoding="utf-8") == "원본"
    assert json.loads(file.read_text(encoding="utf-8"))[0]["thread_id"] == "111"


def test_write_failures(tmp_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    file = tmp_dir / "sessions.json"
    clock = ManualClock(START)
    store = make(file, clock)
    store.set("111", "sess-1")

    def boom(_file: Path, _value: object) -> None:
        raise OSError("디스크 가득")

    monkeypatch.setattr(sessions_module, "write_json", boom)
    with pytest.raises(OSError):
        store.set("222", "sess-2")
    clock.advance(SESSION_TTL.total_seconds() + 5)
    assert store.get("111") is None  # 예외 없음


def test_entry_model_is_strict_and_frozen() -> None:
    good = entry("111", "sess-1", START)
    SessionEntry.model_validate({**good, "extra": 1})
    for bad in (
        {**good, "thread_id": "t1"},
        {**good, "session_id": "-x"},
        {**good, "updated_at": "쓰레기"},
        {**good, "thread_id": 111},
    ):
        with pytest.raises(ValueError):
            SessionEntry.model_validate(bad)


def test_state_files_names() -> None:
    names = [getattr(STATE_FILES, f) for f in STATE_FILES.__dataclass_fields__]
    assert len(names) == len(set(names))
    assert STATE_FILES.data_lock == "data.lock"
    assert STATE_FILES.bot_control == "bot-control.json"


@pytest.mark.parametrize("bad", ["a b", "a\nb", "-x", "a\n", ""])
def test_load_drops_bad_session_id(tmp_dir: Path, bad: str) -> None:
    file = tmp_dir / "sessions.json"
    file.write_text(json.dumps([entry("111", bad, START), entry("222", "ok", START)]))
    store = make(file, ManualClock(START))
    assert len(store) == 1
    assert store.get("111") is None


def test_exact_ttl_boundary_is_kept(tmp_dir: Path) -> None:
    file = tmp_dir / "sessions.json"
    clock = ManualClock(START)
    store = make(file, clock)
    store.set("111", "s1")
    clock.advance(SESSION_TTL.total_seconds())
    assert store.get("111") == "s1"
    assert len(make(file, clock)) == 1  # 읽을 때도 정확히 7일은 남는다
    clock.advance(1)
    assert len(make(file, clock)) == 0


def test_duplicate_tie_later_in_file_wins(tmp_dir: Path) -> None:
    file = tmp_dir / "sessions.json"
    rows = [entry("111", "first", START), entry("111", "second", START)]
    file.write_text(json.dumps(rows))
    assert make(file, ManualClock(START)).get("111") == "second"


@pytest.mark.parametrize("bad", [0, -1])
def test_max_entries_must_be_positive(tmp_dir: Path, bad: int) -> None:
    with pytest.raises(ValueError):
        make(tmp_dir / "s.json", ManualClock(START), max_entries=bad)
