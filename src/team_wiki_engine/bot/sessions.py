"""스레드 → Claude 세션 저장소(7일, 1000개, 원자적 쓰기).

한 프로세스에 저장소는 하나다. 모든 항목을 메모리에 들고 있다가 쓸 때마다 파일 전체를 다시
쓴다(다른 프로세스가 같은 파일을 쓰면 서로 덮어쓴다). 단일 작성자는 `data.lock`(Task 10)이
보장한다. 읽을 때는 파일을 고쳐 쓰지 않는다.

파일 형식: `[{"thread_id", "session_id", "updated_at"(KST ISO)}, …]`.
"""

import logging
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Final

from pydantic import (
    BaseModel,
    ConfigDict,
    FailFast,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
)

from team_wiki_engine.bot.text import is_snowflake
from team_wiki_engine.metrics.record import read_json, write_json
from team_wiki_engine.util.time import kst_iso, parse_instant

log = logging.getLogger(__name__)

SESSION_TTL: Final = timedelta(days=7)
MAX_SESSIONS: Final = 1000
MAX_FUTURE_SKEW: Final = timedelta(days=1)
SESSION_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")

_ADAPTER: Final[TypeAdapter[list[Any]]] = TypeAdapter(
    Annotated[list[Any], FailFast(), Field(max_length=MAX_SESSIONS * 4)]
)


class SessionEntry(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="ignore")

    thread_id: str
    session_id: str
    updated_at: str

    @field_validator("thread_id")
    @classmethod
    def _thread(cls, value: str) -> str:
        if not is_snowflake(value):
            raise ValueError("thread_id 형식이 아닙니다")
        return value

    @field_validator("session_id")
    @classmethod
    def _session(cls, value: str) -> str:
        if SESSION_ID.fullmatch(value) is None:
            raise ValueError("session_id 형식이 아닙니다")
        return value

    @field_validator("updated_at")
    @classmethod
    def _updated(cls, value: str) -> str:
        if parse_instant(value) is None:
            raise ValueError("updated_at을 읽을 수 없습니다")
        return value


class SessionStore:
    def __init__(
        self,
        file: Path,
        *,
        now: Callable[[], datetime],
        ttl: timedelta = SESSION_TTL,
        max_entries: int = MAX_SESSIONS,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries는 1 이상이어야 합니다")
        self._file = file
        self._now = now
        self._ttl = ttl
        self._max = max_entries
        self._entries: dict[str, SessionEntry] = {}
        self._load()

    # ------------------------------------------------------------ 내부
    def _time(self, entry: SessionEntry) -> datetime:
        parsed = parse_instant(entry.updated_at)
        assert parsed is not None  # 검증기가 보장한다
        return parsed

    def _expired(self, entry: SessionEntry, now: datetime) -> bool:
        return now - self._time(entry) > self._ttl

    def _load(self) -> None:
        now = self._now()
        best: dict[str, SessionEntry] = {}
        for raw in read_json(self._file, _ADAPTER, []):
            try:
                item = SessionEntry.model_validate(raw)
            except ValidationError:
                continue
            at = self._time(item)
            if now - at > self._ttl or at - now > MAX_FUTURE_SKEW:
                continue
            old = best.get(item.thread_id)
            if old is None or at >= self._time(old):
                best.pop(item.thread_id, None)
                best[item.thread_id] = item
        ordered = sorted(best.values(), key=self._time)  # 같은 시각이면 파일 순서가 남는다
        self._entries = {e.thread_id: e for e in ordered[-self._max :]}

    def _write(self) -> None:
        write_json(self._file, [e.model_dump() for e in self._entries.values()])

    # ------------------------------------------------------------ 공개
    def __len__(self) -> int:
        return len(self._entries)

    def get(self, thread_id: str) -> str | None:
        entry = self._entries.get(thread_id)
        if entry is None:
            return None
        if self._expired(entry, self._now()):
            del self._entries[thread_id]
            try:
                self._write()
            except OSError:
                log.warning("세션 파일을 다시 쓰지 못했습니다")
            return None
        return entry.session_id

    def set(self, thread_id: str, session_id: str) -> None:
        if not is_snowflake(thread_id) or SESSION_ID.fullmatch(session_id) is None:
            raise ValueError("스레드 ID나 세션 ID 형식이 올바르지 않습니다")
        now = self._now()
        self._entries.pop(thread_id, None)
        self._entries[thread_id] = SessionEntry(
            thread_id=thread_id, session_id=session_id, updated_at=kst_iso(now)
        )
        for key in [k for k, e in self._entries.items() if self._expired(e, now)]:
            del self._entries[key]
        if len(self._entries) > self._max:
            ordered = sorted(self._entries.values(), key=self._time)
            self._entries = {e.thread_id: e for e in ordered[-self._max :]}
        self._write()

    def remove(self, thread_id: str) -> None:
        if self._entries.pop(thread_id, None) is not None:
            self._write()
