"""`data` 사본 주기 갱신. 갱신은 `DataGate`의 쓰기 쪽에서만 돈다.

프로세스 사이 배타성은 `data.lock`(봇이 수명 내내 쥔다)이 맡는다. 운영 알림은 쓰기 관문을 나온
**뒤에** 보낸다(알림 전송 동안 읽기를 막지 않게).
"""

import asyncio
import logging
import os
import stat
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Final

from team_wiki_engine.bot.data_gate import DataGate
from team_wiki_engine.git.client import GitError
from team_wiki_engine.git.ops import CloneStateError
from team_wiki_engine.ops.notify import Notifier
from team_wiki_engine.sync.stage import Mask, describe_error

_log = logging.getLogger(__name__)

REFRESH_INTERVAL_S: Final = 600.0
NOT_READY_INTERVAL_S: Final = 60.0
REFRESH_TIMEOUT_S: Final = 180.0
NOTIFY_EVERY_FAILURES: Final = 6
_TIMEOUT_TEXT: Final = "시간 초과"
_RECOVERED_TEXT: Final = "[bot] data 사본 갱신 회복"


def data_ready(data_dir: Path) -> bool:
    """`data_dir`·`.git`·`wiki`가 모두 링크가 아닌 폴더인가. 예외 없음."""
    for path in (data_dir, data_dir / ".git", data_dir / "wiki"):
        try:
            if not stat.S_ISDIR(os.lstat(path).st_mode):
                return False
        except OSError:
            return False
    return True


@dataclass(frozen=True)
class RefreshState:
    last_ok_at: datetime | None = None
    last_attempt_at: datetime | None = None
    consecutive_failures: int = 0
    last_error: str | None = None


class DataRefresher:
    def __init__(
        self,
        *,
        refresh: Callable[[], Awaitable[None]],
        gate: DataGate,
        data_dir: Path,
        notifier: Notifier,
        mask: Mask,
        now: Callable[[], datetime],
        interval_s: float = REFRESH_INTERVAL_S,
        not_ready_interval_s: float = NOT_READY_INTERVAL_S,
        timeout_s: float = REFRESH_TIMEOUT_S,
    ) -> None:
        self._refresh = refresh
        self._gate = gate
        self._data_dir = data_dir
        self._notifier = notifier
        self._mask = mask
        self._now = now
        self._interval_s = interval_s
        self._not_ready_interval_s = not_ready_interval_s
        self._timeout_s = timeout_s
        self.state = RefreshState()

    async def refresh_once(self) -> bool:
        self.state = replace(self.state, last_attempt_at=self._now())
        error: str | None = None
        try:
            async with self._gate.writing():
                async with asyncio.timeout(self._timeout_s):
                    await self._refresh()
        except TimeoutError:  # OSError의 하위 클래스라 가장 먼저 잡는다
            error = _TIMEOUT_TEXT
        except (GitError, CloneStateError, ValueError, OSError) as exc:
            error = describe_error(exc, self._mask)
        except Exception as exc:
            error = type(exc).__name__
        # 여기부터는 쓰기 관문 밖이다.
        if error is None:
            recovered = self.state.consecutive_failures > 0
            self.state = RefreshState(
                last_ok_at=self._now(),
                last_attempt_at=self.state.last_attempt_at,
                consecutive_failures=0,
                last_error=None,
            )
            if recovered:
                await self._send(_RECOVERED_TEXT, mention_admin=False)
            return True
        failures = self.state.consecutive_failures + 1
        self.state = replace(self.state, consecutive_failures=failures, last_error=error)
        if failures == 1 or failures % NOTIFY_EVERY_FAILURES == 0:
            await self._send(
                f"[bot] data 사본 갱신 실패(연속 {failures}회): {error}", mention_admin=True
            )
        return False

    async def _send(self, text: str, *, mention_admin: bool) -> None:
        try:
            await self._notifier.send(text, mention_admin=mention_admin)
        except Exception:
            _log.warning("data 사본 갱신 알림을 보내지 못했습니다")

    async def run_forever(self) -> None:
        while True:
            ready = data_ready(self._data_dir)
            await asyncio.sleep(self._interval_s if ready else self._not_ready_interval_s)
            await self.refresh_once()
