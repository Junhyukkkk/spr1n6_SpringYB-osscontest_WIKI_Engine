"""라이브 스모크 보강용 도우미: SDK 고정 값, usage 중복 제거 가정 검사, 새 파일 관찰."""

import asyncio
import os
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from pathlib import Path
from typing import Final

PINNED_SDK: Final = "0.2.164"
"""이 값을 올리는 커밋은 같은 버전으로 실제 SDK 스모크(맥과 서버)를 다시 통과한 기록과 함께여야
한다 — 계획 4 실행 기록"""

AFTER_STOP_S: Final = 5.0


def usage_regressions(records: Sequence[tuple[str | None, Mapping[str, int]]]) -> list[str]:
    """같은 `message_id`의 기록들 사이에서 `input_tokens`가 바뀌거나 `output_tokens`가 줄어든 id.

    `None` id는 무시한다. 러너의 "id마다 마지막 usage" 가정은 이 목록이 빌 때 맞다.
    """
    last: dict[str, Mapping[str, int]] = {}
    bad: list[str] = []
    for message_id, usage in records:
        if message_id is None:
            continue
        prev = last.get(message_id)
        if prev is not None and message_id not in bad:
            if prev.get("input_tokens") != usage.get("input_tokens") or usage.get(
                "output_tokens", 0
            ) < prev.get("output_tokens", 0):
                bad.append(message_id)
        last[message_id] = usage
    return bad


async def watch_new_files(
    directory: Path,
    known: Collection[str],
    *,
    t0: float,
    stop: Callable[[], bool],
    interval_s: float = 0.25,
    max_s: float = 45.0,
) -> dict[str, float]:
    """`interval_s`마다 `directory`를 나열해 `known`에 없는 이름이 처음 보인 시각을 모은다.

    시각은 `time.monotonic() - t0`. `stop()`이 참이 된 뒤에도 5초 더 보고, `max_s`(감시 시작
    기준)가 지나면 끝낸다. 폴더가 없으면 빈 결과.
    """
    seen: dict[str, float] = {}
    start = time.monotonic()
    stopped_at: float | None = None
    while True:
        now = time.monotonic()
        try:
            names = os.listdir(directory)
        except OSError:
            names = []
        for name in names:
            if name not in known and name not in seen:
                seen[name] = now - t0
        if stopped_at is None and stop():
            stopped_at = now
        if stopped_at is not None and now - stopped_at >= AFTER_STOP_S:
            break
        if now - start >= max_s:
            break
        await asyncio.sleep(interval_s)
    return seen
