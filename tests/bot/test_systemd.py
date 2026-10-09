import os
import shutil
import socket
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from team_wiki_engine.bot.systemd import sd_notify, watchdog_interval


@pytest.fixture
def sock_dir() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(dir="/tmp"))  # macOS 소켓 경로 104바이트 제한
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_sd_notify_sends_datagram(sock_dir: Path) -> None:
    path = sock_dir / "n.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        server.bind(str(path))
        server.settimeout(2)
        assert sd_notify("READY=1", {"NOTIFY_SOCKET": str(path)}) is True
        assert server.recv(100) == b"READY=1"
    finally:
        server.close()


def test_sd_notify_failures_are_quiet(sock_dir: Path) -> None:
    assert sd_notify("READY=1", {}) is False
    assert sd_notify("READY=1", {"NOTIFY_SOCKET": ""}) is False
    assert sd_notify("READY=1", {"NOTIFY_SOCKET": str(sock_dir / "none.sock")}) is False
    assert sd_notify("READY=1", {"NOTIFY_SOCKET": "a\0b"}) is False
    assert sd_notify("READY=1", {"NOTIFY_SOCKET": "@" + "x" * 500}) is False


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="추상 이름은 리눅스만")
def test_sd_notify_abstract_name() -> None:
    name = f"twe-test-{os.getpid()}"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        server.bind("\0" + name)
        server.settimeout(2)
        assert sd_notify("WATCHDOG=1", {"NOTIFY_SOCKET": "@" + name}) is True
        assert server.recv(100) == b"WATCHDOG=1"
    finally:
        server.close()


def test_watchdog_interval() -> None:
    assert watchdog_interval({"WATCHDOG_USEC": "60000000"}) == 30.0
    assert (
        watchdog_interval({"WATCHDOG_USEC": "60000000", "WATCHDOG_PID": str(os.getpid())}) == 30.0
    )
    assert watchdog_interval({"WATCHDOG_USEC": "60000000", "WATCHDOG_PID": "1"}) is None
    assert watchdog_interval({"WATCHDOG_USEC": "60000000", "WATCHDOG_PID": "x"}) is None
    for bad in ("abc", "0", "-5", "", "1" * 20, "5\n"):
        assert watchdog_interval({"WATCHDOG_USEC": bad}) is None
    assert watchdog_interval({}) is None
