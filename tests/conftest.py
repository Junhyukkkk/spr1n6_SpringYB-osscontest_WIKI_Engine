import ipaddress
import shutil
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import discord
import pytest
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

from tests.helpers import make_raw_tmp_dir, make_tmp_dir
from tests.helpers.offline import OfflineGuard, is_live_test


@pytest.fixture
def tmp_dir() -> Iterator[Path]:
    path = make_tmp_dir()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def raw_tmp_dir() -> Iterator[Path]:
    path = make_raw_tmp_dir()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------- 오프라인 안전장치

_NETWORK_FAMILIES = (socket.AF_INET, socket.AF_INET6)
_NETWORK_MESSAGE = "테스트에서 네트워크 연결은 금지입니다"


def _host_is_local(host: object) -> bool:
    if host is None:
        return True
    if isinstance(host, (bytes, bytearray)):
        try:
            host = bytes(host).decode("ascii")
        except UnicodeDecodeError:
            return False
    if not isinstance(host, str):
        return False
    if host == "localhost":
        return True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _describe_address(address: object) -> str:
    # 주소만 적는다(자격 증명이 들어갈 자리가 없다).
    if isinstance(address, tuple) and len(address) >= 2:
        return f"{address[0]}:{address[1]}"
    return "?"


@pytest.fixture(autouse=True)
def _offline_guard(request: pytest.FixtureRequest) -> Iterator[OfflineGuard]:
    """`live` 마커가 없는 테스트에서 실제 네트워크와 실제 Claude 호출을 막고 기록한다.

    러너가 예외를 결과로 삼켜도 정리 단계에서 기록이 남아 있으면 테스트를 실패시킨다.
    기록을 일부러 확인하는 테스트는 확인한 뒤 `attempts`를 비운다.
    """
    guard = OfflineGuard()
    if is_live_test(request.node):
        yield guard
        return

    def blocked_query(*args: Any, **kwargs: Any) -> Any:
        guard.attempts.append("Claude query 호출")
        raise RuntimeError("테스트에서 실제 Claude 호출은 금지입니다")

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def connect(self: socket.socket, address: Any) -> None:
        if self.family in _NETWORK_FAMILIES:
            guard.attempts.append(f"connect {_describe_address(address)}")
            raise RuntimeError(_NETWORK_MESSAGE)
        original_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> int:
        if self.family in _NETWORK_FAMILIES:
            guard.attempts.append(f"connect_ex {_describe_address(address)}")
            raise RuntimeError(_NETWORK_MESSAGE)
        return original_connect_ex(self, address)

    original_getaddrinfo = socket.getaddrinfo

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if not _host_is_local(host):
            guard.attempts.append("getaddrinfo 호스트 이름 조회")
            raise RuntimeError(_NETWORK_MESSAGE)
        return original_getaddrinfo(host, *args, **kwargs)

    async def blocked_cli_connect(self: Any) -> None:
        guard.attempts.append("Claude CLI 전송 연결")
        raise RuntimeError("테스트에서 실제 Claude 호출은 금지입니다")

    async def blocked_discord(self: Any, *args: Any, **kwargs: Any) -> None:
        guard.attempts.append("디스코드 연결")
        raise RuntimeError("테스트에서 디스코드 연결은 금지입니다")

    def guarded_lookup(name: str, original: Any, host_of: Any) -> Any:
        def lookup(*args: Any, **kwargs: Any) -> Any:
            if not _host_is_local(host_of(args, kwargs)):
                guard.attempts.append(f"{name} 호스트 이름 조회")
                raise RuntimeError(_NETWORK_MESSAGE)
            return original(*args, **kwargs)

        return lookup

    def first_arg(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        return args[0] if args else next(iter(kwargs.values()), None)

    def sockaddr_host(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        sockaddr = first_arg(args, kwargs)
        return sockaddr[0] if isinstance(sockaddr, tuple) and sockaddr else None

    with pytest.MonkeyPatch.context() as mp:
        for lookup_name in ("gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
            mp.setattr(
                socket,
                lookup_name,
                guarded_lookup(lookup_name, getattr(socket, lookup_name), first_arg),
            )
        mp.setattr(
            socket, "getnameinfo", guarded_lookup("getnameinfo", socket.getnameinfo, sockaddr_host)
        )
        mp.setattr(SubprocessCLITransport, "connect", blocked_cli_connect)
        mp.setattr(discord.Client, "login", blocked_discord)
        mp.setattr(discord.Client, "connect", blocked_discord)
        mp.setattr(socket.socket, "connect", connect)
        mp.setattr(socket.socket, "connect_ex", connect_ex)
        mp.setattr(socket, "getaddrinfo", getaddrinfo)
        mp.setattr("team_wiki_engine.claude.runner.query", blocked_query)
        mp.setattr("claude_agent_sdk.query", blocked_query)
        # git의 https 전송은 연결 전에 거부된다(하위 프로세스라 기록되지 않고 GitError로 드러난다).
        mp.setattr("team_wiki_engine.git.client.DEFAULT_ALLOWED_PROTOCOLS", "file")
        yield guard
    if guard.attempts:
        pytest.fail("네트워크/Claude 호출 시도: " + ", ".join(guard.attempts))


@pytest.fixture
def offline_guard(_offline_guard: OfflineGuard) -> OfflineGuard:
    """막은 시도의 기록. 확인한 테스트는 `attempts`를 비워야 정리 단계에서 실패하지 않는다."""
    return _offline_guard
