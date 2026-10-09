"""systemd 상태 신호(`sd_notify`)와 워치독. 예외를 내지 않고, 의존성도 없다."""

import asyncio
import os
import re
import socket
from collections.abc import Mapping
from typing import Final

_USEC: Final = re.compile(r"[0-9]{1,15}")
_PID: Final = re.compile(r"[0-9]{1,15}")


def sd_notify(state: str, env: Mapping[str, str]) -> bool:
    address = env.get("NOTIFY_SOCKET", "")
    if not address:
        return False
    target = "\0" + address[1:] if address.startswith("@") else address  # 추상 이름은 리눅스만
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.setblocking(False)
            sock.sendto(state.encode(), target)
        finally:
            sock.close()
    except (OSError, ValueError):
        return False
    return True


def watchdog_interval(env: Mapping[str, str]) -> float | None:
    usec = env.get("WATCHDOG_USEC", "")
    if _USEC.fullmatch(usec) is None or int(usec) <= 0:
        return None
    pid = env.get("WATCHDOG_PID")
    if pid is not None and (_PID.fullmatch(pid) is None or int(pid) != os.getpid()):
        return None
    return int(usec) / 2 / 1_000_000


async def watchdog_loop(env: Mapping[str, str], interval_s: float) -> None:
    while True:
        sd_notify("WATCHDOG=1", env)
        await asyncio.sleep(interval_s)
