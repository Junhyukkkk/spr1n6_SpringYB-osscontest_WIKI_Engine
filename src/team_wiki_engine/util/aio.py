"""asyncio 도우미: 바깥이 취소돼도 안쪽 작업은 끝까지 돌린다.

git 변경 작업(reset·clean·commit)이 도중에 취소되면 `GitClient`가 프로세스 그룹을 `SIGKILL`해
`.git/index.lock`이 남고 작업 사본이 반쯤 바뀐 채로 끝난다. `asyncio.shield`는 두 번째 취소에서
바로 던지므로 쓰지 않고, 안쪽 태스크가 끝날 때까지 취소를 몇 번이든 미룬다.
"""

import asyncio
from collections.abc import Coroutine
from typing import Any


async def settle[T](aw: Coroutine[Any, Any, T]) -> tuple[asyncio.Task[T], bool]:
    """`aw`를 별도 태스크로 끝까지 돌린다. 끝난 태스크와 '도중에 취소가 왔는가'를 돌려준다.

    안쪽 예외는 던지지 않고 태스크에 남는다(`task.exception()`으로 읽는다).
    """
    task = asyncio.ensure_future(aw)
    cancelled = False
    while not task.done():
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError:
            cancelled = True
    return task, cancelled


async def run_to_completion[T](aw: Coroutine[Any, Any, T]) -> T:
    """`aw`를 끝까지 돌린 뒤 결과를 돌려준다. 그동안 취소가 왔으면 끝난 다음 취소를 던진다.

    취소와 안쪽 실패가 겹치면 취소가 이긴다(안쪽 예외는 회수만 한다).
    """
    task, cancelled = await settle(aw)
    if cancelled:
        if not task.cancelled():
            task.exception()  # 미회수 예외 경고 방지
        raise asyncio.CancelledError
    return task.result()
