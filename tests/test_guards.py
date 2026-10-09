"""테스트 오프라인 안전장치(`tests/conftest.py`의 `_offline_guard`)가 켜져 있는지 확인한다."""

import socket
import sys
from pathlib import Path
from typing import Any

import pytest

from team_wiki_engine.claude.runner import RunnerDeps, create_claude_runner
from team_wiki_engine.claude.types import RunClaudeInput
from tests.helpers import write_tree
from tests.helpers.offline import OfflineGuard, is_live_test

pytest_plugins = ["pytester"]

REPO_SKILLS = Path(__file__).resolve().parents[1] / "skills"


def test_tcp_connect_is_blocked_and_recorded(offline_guard: OfflineGuard) -> None:
    with pytest.raises(RuntimeError, match="네트워크 연결은 금지"):
        socket.create_connection(("127.0.0.1", 9), timeout=1)
    assert len(offline_guard.attempts) >= 1
    offline_guard.attempts.clear()


def test_connect_ex_is_blocked_and_recorded(offline_guard: OfflineGuard) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s, pytest.raises(RuntimeError):
        s.connect_ex(("127.0.0.1", 9))
    assert len(offline_guard.attempts) == 1
    offline_guard.attempts.clear()


def test_name_lookup_of_a_remote_host_is_blocked(offline_guard: OfflineGuard) -> None:
    with pytest.raises(RuntimeError):
        socket.getaddrinfo("example.com", 443)
    assert len(offline_guard.attempts) == 1
    offline_guard.attempts.clear()


def test_localhost_and_numeric_hosts_are_not_blocked(offline_guard: OfflineGuard) -> None:
    assert socket.getaddrinfo("localhost", 0)
    assert socket.getaddrinfo("127.0.0.1", 0)
    assert socket.getaddrinfo("::1", 0)
    assert socket.getaddrinfo(None, 0)
    assert offline_guard.attempts == []


def test_unix_socketpair_still_works(offline_guard: OfflineGuard) -> None:
    a, b = socket.socketpair()
    try:
        a.sendall(b"x")
        assert b.recv(1) == b"x"
    finally:
        a.close()
        b.close()
    assert offline_guard.attempts == []


async def test_real_sdk_query_is_blocked_even_when_the_runner_swallows_the_error(
    tmp_dir: Path, offline_guard: OfflineGuard
) -> None:
    write_tree(tmp_dir, {"data/wiki/index.md": "# 목차"})
    deps = RunnerDeps(
        model="m",
        effort={"bot": "low", "sync": "low"},
        skills_dir=REPO_SKILLS,
        env={},
        query_fn=None,
    )
    inp = RunClaudeInput(
        profile="bot",
        prompt="질문",
        cwd=tmp_dir / "data",
        read_roots=(),
        write_roots=(),
        timeout_ms=5_000,
    )
    result = await create_claude_runner(deps)(inp)
    assert result.ok is False  # 옵션 조립을 지나 query까지 가서 막혔다
    assert result.failure == "error"
    assert len(offline_guard.attempts) == 1
    offline_guard.attempts.clear()


def test_sdk_module_level_query_is_blocked_too(offline_guard: OfflineGuard) -> None:
    import claude_agent_sdk

    with pytest.raises(RuntimeError):
        claude_agent_sdk.query(prompt="x")
    assert len(offline_guard.attempts) == 1
    offline_guard.attempts.clear()


class _StubItem:
    def __init__(self, marks: set[str]) -> None:
        self._marks = marks

    def get_closest_marker(self, name: str) -> object | None:
        return object() if name in self._marks else None


@pytest.fixture
def live_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIVE_CLAUDE", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")


@pytest.fixture
def live_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIVE_CLAUDE", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.mark.usefixtures("live_env")
def test_is_live_test_is_true_only_for_the_live_marker_when_live_is_enabled() -> None:
    assert is_live_test(_StubItem({"live"}))  # type: ignore[arg-type]
    assert not is_live_test(_StubItem(set()))  # type: ignore[arg-type]
    assert not is_live_test(_StubItem({"asyncio", "skipif"}))  # type: ignore[arg-type]


@pytest.mark.usefixtures("live_disabled")
def test_live_marker_alone_does_not_exempt_when_live_is_disabled() -> None:
    assert not is_live_test(_StubItem({"live"}))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("live_claude", "api_key"),
    [(None, "k"), ("0", "k"), ("1", ""), ("1", "   "), ("1", None), ("true", "k")],
)
def test_live_needs_both_live_claude_1_and_a_non_blank_key(
    monkeypatch: pytest.MonkeyPatch, live_claude: str | None, api_key: str | None
) -> None:
    for name, value in (("LIVE_CLAUDE", live_claude), ("ANTHROPIC_API_KEY", api_key)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    assert not is_live_test(_StubItem({"live"}))  # type: ignore[arg-type]


def test_this_test_is_not_live(request: pytest.FixtureRequest) -> None:
    assert not is_live_test(request.node)


# ---------------------------------------------------------------- 안전장치 메타 테스트

INNER_CONFTEST = "from tests.conftest import _offline_guard, offline_guard  # noqa: F401\n"
INNER_INI = "[pytest]\nmarkers =\n    live: live\nasyncio_default_fixture_loop_scope = function\n"
SWALLOWING_TEST = """
import socket
{decorator}
def test_swallows():
    try:
        socket.create_connection(("127.0.0.1", 9), timeout=1)
    except RuntimeError:
        pass
"""
OUTER_GETADDRINFO: Any = None


def _inner(pytester: pytest.Pytester, source: str) -> pytest.RunResult:
    pytester.makeini(INNER_INI)
    pytester.makeconftest(INNER_CONFTEST)
    pytester.makepyfile(source)
    return pytester.runpytest()


def test_a_test_that_swallows_the_guard_error_still_fails_at_teardown(
    pytester: pytest.Pytester,
) -> None:
    result = _inner(pytester, SWALLOWING_TEST.format(decorator=""))
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*네트워크/Claude 호출 시도*"])


@pytest.mark.usefixtures("live_disabled")
def test_live_marked_test_stays_guarded_when_live_is_disabled(pytester: pytest.Pytester) -> None:
    result = _inner(pytester, SWALLOWING_TEST.format(decorator="import pytest\n@pytest.mark.live"))
    result.assert_outcomes(passed=1, errors=1)


@pytest.mark.usefixtures("live_env")
def test_live_marked_test_is_exempt_when_live_is_enabled(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 장치가 꺼져 있으면 getaddrinfo는 바깥(이 테스트의) 감싼 함수 그대로다.
    monkeypatch.setattr(sys.modules[__name__], "OUTER_GETADDRINFO", socket.getaddrinfo)
    source = f"""
import socket
import pytest
import {__name__} as outer

@pytest.mark.live
def test_live():
    assert socket.getaddrinfo is outer.OUTER_GETADDRINFO

def test_not_live():
    assert socket.getaddrinfo is not outer.OUTER_GETADDRINFO
"""
    _inner(pytester, source).assert_outcomes(passed=2)


# ---------------------------------------------------------------- 더 막는 경로


async def test_sdk_cli_transport_connect_is_blocked_and_recorded(
    offline_guard: OfflineGuard,
) -> None:
    from claude_agent_sdk import ClaudeAgentOptions
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

    transport = SubprocessCLITransport(prompt="x", options=ClaudeAgentOptions())
    with pytest.raises(RuntimeError):
        await transport.connect()
    assert len(offline_guard.attempts) == 1
    offline_guard.attempts.clear()


@pytest.mark.parametrize(
    "call",
    [
        lambda: socket.gethostbyname("example.com"),
        lambda: socket.gethostbyname_ex("example.com"),
        lambda: socket.gethostbyaddr("example.com"),
        lambda: socket.getnameinfo(("example.com", 80), 0),
    ],
)
def test_other_name_lookups_of_remote_hosts_are_blocked(
    offline_guard: OfflineGuard, call: Any
) -> None:
    with pytest.raises(RuntimeError):
        call()
    assert len(offline_guard.attempts) == 1
    offline_guard.attempts.clear()


def test_name_lookups_of_local_hosts_are_not_blocked(offline_guard: OfflineGuard) -> None:
    assert socket.gethostbyname("127.0.0.1") == "127.0.0.1"
    assert socket.gethostbyname("localhost")
    assert offline_guard.attempts == []


def test_unix_socket_connect_is_not_blocked(tmp_dir: Path, offline_guard: OfflineGuard) -> None:
    path = str(tmp_dir / "s")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(path)
        server.listen(1)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(path)
            assert client.connect_ex(path) in (0, 106, 56)  # 이미 연결됨(EISCONN)이어도 막지 않는다
    assert offline_guard.attempts == []


# ---------------------------------------------------------------- 세 장치가 한꺼번에 켜져 있다


def test_all_devices_are_on_at_once(offline_guard: OfflineGuard) -> None:
    """소켓·이름 조회, `query` 두 곳, git 전송 `file`이 한 테스트 안에서 모두 켜져 있다."""
    import claude_agent_sdk

    from team_wiki_engine.claude import runner
    from team_wiki_engine.git import client

    # 1) 소켓과 이름 조회.
    with pytest.raises(RuntimeError):
        socket.create_connection(("127.0.0.1", 9), timeout=1)
    with pytest.raises(RuntimeError):
        socket.getaddrinfo("example.com", 443)
    # 2) 실제 Claude 호출 경로 두 곳.
    with pytest.raises(RuntimeError):
        runner.query(prompt="x", options=None)  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError):
        claude_agent_sdk.query(prompt="x")
    # 3) git 전송은 file만.
    assert client.DEFAULT_ALLOWED_PROTOCOLS == "file"
    # 4) 디스코드 연결은 login·connect가 막힌 가짜다(실제 호출은 비동기 시험이 따로 확인한다).
    import discord

    assert discord.Client.login.__name__ == "blocked_discord"
    assert discord.Client.connect.__name__ == "blocked_discord"

    assert len(offline_guard.attempts) == 4
    offline_guard.attempts.clear()


async def test_discord_login_and_connect_are_blocked(offline_guard: OfflineGuard) -> None:
    import discord

    client = discord.Client(intents=discord.Intents.none())
    with pytest.raises(RuntimeError, match="디스코드 연결은 금지"):
        await client.login("x")
    with pytest.raises(RuntimeError, match="디스코드 연결은 금지"):
        await client.connect()
    assert offline_guard.attempts == ["디스코드 연결", "디스코드 연결"]
    offline_guard.attempts.clear()
