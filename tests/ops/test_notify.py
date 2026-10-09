import asyncio
import json
import time
from typing import Any

import httpx2
import pytest

from team_wiki_engine.ops import notify as notify_module
from team_wiki_engine.ops.notify import (
    DISCORD_API,
    DISCORD_LIMIT,
    DiscordNotifier,
    Notifier,
    redact_secrets,
    truncate_utf16,
)
from tests.helpers.hang_guard import hang_guard
from tests.helpers.http import Handler, idle_client, mock_client
from tests.helpers.notify import RecordingNotifier

CHANNEL = "123456789012345678"
ADMIN = "987654321098765432"


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


class Env:
    def __init__(self, handler: Handler | None = None) -> None:
        self.requests: list[httpx2.Request] = []
        self.logs: list[str] = []
        self._handler = handler or (lambda request: httpx2.Response(200, json={}))
        self.client = mock_client(self._handle)

    async def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        result = self._handler(request)
        if isinstance(result, httpx2.Response):
            return result
        return await result

    def notifier(
        self,
        *,
        token: str | None = "bot-token-xyz",
        channel_id: str = CHANNEL,
        admin_user_id: str = ADMIN,
        client: bool = True,
        redact: tuple[str, ...] = (),
    ) -> DiscordNotifier:
        return DiscordNotifier(
            token=token,
            channel_id=channel_id,
            admin_user_id=admin_user_id,
            client=self.client if client else None,
            log=self.logs.append,
            redact=redact,
        )

    def body(self, index: int = 0) -> dict[str, Any]:
        loaded = json.loads(self.requests[index].content)
        assert isinstance(loaded, dict)
        return loaded


@pytest.fixture
async def env() -> Any:
    e = Env()
    try:
        yield e
    finally:
        await e.client.aclose()


def test_constants() -> None:
    assert DISCORD_API == "https://discord.com/api/v10"
    assert DISCORD_LIMIT == 2000


async def test_sends_without_mention(env: Env) -> None:
    await env.notifier().send("보고")
    assert len(env.requests) == 1
    request = env.requests[0]
    assert request.method == "POST"
    assert str(request.url) == f"{DISCORD_API}/channels/{CHANNEL}/messages"
    assert request.headers["authorization"] == "Bot bot-token-xyz"
    assert request.headers["user-agent"] == "team-wiki-engine"
    assert request.headers["content-type"] == "application/json"
    assert env.body() == {"content": "보고", "allowed_mentions": {"parse": [], "users": []}}
    assert env.logs == ["[ops] 보고"]
    assert "bot-token-xyz" not in "".join(env.logs)


async def test_mention_pings_only_the_admin(env: Env) -> None:
    await env.notifier().send("실패", mention_admin=True)
    assert env.body() == {
        "content": f"실패 <@{ADMIN}>",
        "allowed_mentions": {"parse": [], "users": [ADMIN]},
    }
    assert env.logs == [f"[ops] 실패 <@{ADMIN}>"]


async def test_log_only_without_token_or_channel(env: Env) -> None:
    await env.notifier(token=None).send("a")
    await env.notifier(token="").send("b")
    await env.notifier(channel_id="").send("c")
    assert env.requests == []
    assert env.logs == ["[ops] a", "[ops] b", "[ops] c"]


async def test_failures_are_logged_not_raised() -> None:
    def boom(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("network down")

    down = Env(boom)
    broken = Env(lambda request: httpx2.Response(500, text="oops body"))
    try:
        await down.notifier().send("x")
        await broken.notifier().send("y")
    finally:
        await down.client.aclose()
        await broken.client.aclose()
    assert down.logs == ["[ops] x", "[ops] 운영 채널 전송 실패: ConnectError"]
    assert broken.logs == ["[ops] y", "[ops] 운영 채널 전송 실패: HTTP 500"]
    assert "oops body" not in "".join(broken.logs)


async def test_truncates_at_2000(env: Env) -> None:
    await env.notifier().send("가" * 2500)
    assert env.body()["content"] == "가" * 2000


async def test_emoji_truncated_by_utf16_without_broken_characters(env: Env) -> None:
    await env.notifier().send("😀" * 2500)
    content = env.body()["content"]
    assert utf16_len(content) <= 2000
    assert content == "😀" * 1000
    content.encode("utf-8")  # 외톨이 서로게이트가 없다
    assert utf16_len(env.logs[0]) <= 2000 + len("[ops] ")


async def test_mention_survives_truncation(env: Env) -> None:
    await env.notifier().send("😀" * 2500, mention_admin=True)
    content = env.body()["content"]
    assert content.endswith(f" <@{ADMIN}>")
    assert utf16_len(content) <= 2000
    assert env.body()["allowed_mentions"]["users"] == [ADMIN]


async def test_known_secrets_and_token_are_redacted_in_log_and_body(env: Env) -> None:
    secret = "ghp_secret_value_123"
    text = f"토큰 {secret} 그리고 bot-token-xyz 끝"
    await env.notifier(redact=(secret,)).send(text)
    assert env.body()["content"] == "토큰 <비밀값> 그리고 <비밀값> 끝"
    assert env.logs == ["[ops] 토큰 <비밀값> 그리고 <비밀값> 끝"]
    assert secret not in "".join(env.logs) + env.requests[0].content.decode()


async def test_redaction_happens_before_truncation(env: Env) -> None:
    secret = "ghp_secret_value_123"
    # 잘리는 경계에 비밀값이 걸려도 앞부분이 새지 않는다.
    await env.notifier(redact=(secret,)).send("a" * 1995 + secret)
    content = env.body()["content"]
    assert "ghp_" not in content
    assert content == "a" * 1995 + "<비밀값>"


@pytest.mark.parametrize("channel_id", ["../../x", "123\n", "abc", "1" * 26, " 123", "١٢٣"])
async def test_bad_channel_id_never_requests(env: Env, channel_id: str) -> None:
    await env.notifier(channel_id=channel_id).send("보고")
    assert env.requests == []
    assert env.logs == ["[ops] 보고"]


@pytest.mark.parametrize("admin", ["abc", "123\n", "", "<@1>", "1" * 26])
async def test_bad_admin_id_never_mentions(env: Env, admin: str) -> None:
    await env.notifier(admin_user_id=admin).send("보고", mention_admin=True)
    assert env.body() == {"content": "보고", "allowed_mentions": {"parse": [], "users": []}}


async def test_exception_message_is_never_logged() -> None:
    def boom(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connect failed for ghp_x token")

    e = Env(boom)
    try:
        await e.notifier().send("x")
    finally:
        await e.client.aclose()
    assert e.logs[-1] == "[ops] 운영 채널 전송 실패: ConnectError"
    assert "ghp_x" not in "".join(e.logs)


async def test_no_client_logs_only(env: Env) -> None:
    await env.notifier(client=False).send("보고")
    assert env.requests == []
    assert env.logs == ["[ops] 보고"]


async def test_redirect_is_not_followed_and_logged_as_failure() -> None:
    e = Env(lambda request: httpx2.Response(302, headers={"location": "https://evil.test/x"}))
    try:
        await e.notifier().send("x")
    finally:
        await e.client.aclose()
    assert len(e.requests) == 1
    assert e.logs[-1] == "[ops] 운영 채널 전송 실패: HTTP 302"


async def test_request_timeout_is_10_seconds(env: Env) -> None:
    await env.notifier().send("x")
    timeout = env.requests[0].extensions["timeout"]
    assert timeout == {"connect": 10.0, "read": 10.0, "write": 10.0, "pool": 10.0}


async def test_outer_cancellation_propagates() -> None:
    started = asyncio.Event()

    async def handler(request: httpx2.Request) -> httpx2.Response:
        started.set()
        await asyncio.Event().wait()
        return httpx2.Response(200)

    client = mock_client(handler)
    notifier = DiscordNotifier(
        token="tok-abcdefgh",
        channel_id=CHANNEL,
        admin_user_id=ADMIN,
        client=client,
        log=lambda s: None,
    )
    task = asyncio.create_task(notifier.send("x"))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert task.cancelled()
    await client.aclose()


async def test_log_callback_failure_does_not_raise() -> None:
    def bad_log(line: str) -> None:
        raise OSError("stdout closed")

    notifier = DiscordNotifier(
        token=None, channel_id=CHANNEL, admin_user_id=ADMIN, client=None, log=bad_log
    )
    await notifier.send("x")


async def test_default_log_prints(capsys: pytest.CaptureFixture[str]) -> None:
    await DiscordNotifier(token=None, channel_id=CHANNEL, admin_user_id=ADMIN, client=None).send(
        "안녕"
    )
    assert capsys.readouterr().out == "[ops] 안녕\n"


async def test_lone_surrogate_does_not_break_sending(env: Env) -> None:
    await env.notifier().send("a\ud800b")
    assert env.body()["content"] == "a�b"


def test_repr_has_no_token() -> None:
    notifier = DiscordNotifier(
        token="super-secret-token",
        channel_id=CHANNEL,
        admin_user_id=ADMIN,
        client=None,
        redact=("another-secret-value",),
    )
    text = repr(notifier) + str(notifier)
    assert "super-secret-token" not in text
    assert "another-secret-value" not in text


def test_notifier_protocol_is_satisfied() -> None:
    discord: Notifier = DiscordNotifier(token=None, channel_id="", admin_user_id="", client=None)
    recording: Notifier = RecordingNotifier()
    assert discord is not None and recording is not None


async def test_recording_notifier_records() -> None:
    recorder = RecordingNotifier()
    await recorder.send("a")
    await recorder.send("b", mention_admin=True)
    assert recorder.sent == [("a", False), ("b", True)]


class TestRedactSecrets:
    def test_replaces_every_occurrence(self) -> None:
        assert redact_secrets("ab secret-value-1 cd secret-value-1", ["secret-value-1"]) == (
            "ab <비밀값> cd <비밀값>"
        )

    def test_ignores_values_shorter_than_8(self) -> None:
        assert redact_secrets("abc 1234567 x", ["abc", "1234567", ""]) == "abc 1234567 x"
        assert redact_secrets("12345678", ["12345678"]) == "<비밀값>"

    def test_longest_first(self) -> None:
        assert redact_secrets("prefix-longer-secret!", ["prefix-lo", "prefix-longer-secret!"]) == (
            "<비밀값>"
        )

    def test_regex_metacharacters_are_literal(self) -> None:
        assert redact_secrets("a.*+?[b](c)|d", [".*+?[b](c)|"]) == "a<비밀값>d"

    def test_no_secrets(self) -> None:
        assert redact_secrets("text", []) == "text"

    def test_replacement_is_single_pass(self) -> None:
        # 치환 결과가 다른 비밀값과 다시 맞지 않는다.
        assert redact_secrets("aaaaaaaa", ["aaaaaaaa", "<비밀값>aaaa"]) == "<비밀값>"


class TestTruncateUtf16:
    def test_short_text_is_unchanged(self) -> None:
        assert truncate_utf16("abc", 10) == "abc"
        assert truncate_utf16("abc", 3) == "abc"

    def test_cuts_by_utf16_units(self) -> None:
        assert truncate_utf16("가" * 10, 4) == "가" * 4
        assert truncate_utf16("😀" * 10, 4) == "😀😀"

    def test_never_splits_a_surrogate_pair(self) -> None:
        assert truncate_utf16("a😀", 2) == "a"
        assert truncate_utf16("a😀", 3) == "a😀"
        assert truncate_utf16("😀", 1) == ""

    def test_zero_or_negative_limit(self) -> None:
        assert truncate_utf16("abc", 0) == ""
        assert truncate_utf16("abc", -5) == ""

    def test_huge_input_is_cheap(self) -> None:
        with hang_guard(5):
            assert utf16_len(truncate_utf16("😀" * 5_000_000, 2000)) == 2000


# ------------------------------------------------------------------ 수정 라운드 1


class TestRedactArgumentTypes:
    @pytest.mark.parametrize("bad", ["ghp_secret_value_123", b"ghp_secret_value_123"])
    def test_redact_secrets_rejects_bare_str_and_bytes(self, bad: Any) -> None:
        with pytest.raises(TypeError) as info:
            redact_secrets("text ghp_secret_value_123", bad)
        assert "ghp_secret" not in str(info.value)

    def test_redact_secrets_rejects_non_str_elements(self) -> None:
        with pytest.raises(TypeError):
            redact_secrets("text", [b"ghp_secret_value_123"])  # type: ignore[list-item]
        with pytest.raises(TypeError):
            redact_secrets("text", [123456789])  # type: ignore[list-item]

    def test_redact_secrets_accepts_generators_and_sets(self) -> None:
        assert (
            redact_secrets("a secret-value-1 b", (s for s in ["secret-value-1"])) == "a <비밀값> b"
        )
        assert redact_secrets("a secret-value-1 b", {"secret-value-1"}) == "a <비밀값> b"

    @pytest.mark.parametrize("bad", ["ghp_secret_value_123", b"ghp_secret_value_123"])
    def test_constructor_rejects_bare_str_and_bytes(self, bad: Any) -> None:
        with pytest.raises(TypeError) as info:
            DiscordNotifier(
                token="tok-abcdefgh",
                channel_id=CHANNEL,
                admin_user_id=ADMIN,
                client=None,
                redact=bad,
            )
        assert "ghp_secret" not in str(info.value)


class TestOverlappingSecrets:
    def test_overlapping_secrets_leave_no_fragment(self) -> None:
        assert redact_secrets("abcdefghijkl", ["abcdefgh", "efghijkl"]) == "<비밀값>"
        assert redact_secrets("abcdefghijkl", ["efghijkl", "abcdefgh"]) == "<비밀값>"

    def test_self_overlap(self) -> None:
        assert redact_secrets("a" * 12, ["a" * 10]) == "<비밀값>"
        assert redact_secrets("x" + "a" * 12 + "y", ["a" * 10]) == "x<비밀값>y"
        assert redact_secrets("a" * 9, ["a" * 10]) == "a" * 9

    def test_adjacent_matches_merge(self) -> None:
        assert redact_secrets("abcdefghABCDEFGH!", ["abcdefgh", "ABCDEFGH"]) == "<비밀값>!"

    def test_contained_secret(self) -> None:
        assert redact_secrets("--abcdefghijkl--", ["abcdefghijkl", "cdefghij"]) == "--<비밀값>--"

    def test_separate_matches_stay_separate(self) -> None:
        assert redact_secrets("abcdefgh-abcdefgh", ["abcdefgh"]) == "<비밀값>-<비밀값>"

    def test_short_secrets_still_ignored_in_overlap(self) -> None:
        assert redact_secrets("abcdefghijkl", ["abcdefgh", "ijkl"]) == "<비밀값>ijkl"

    def test_pathological_repeats_are_fast(self) -> None:
        with hang_guard(5):
            out = redact_secrets("a" * 300_000, ["a" * 8, "a" * 9, "a" * 10])
        assert out == "<비밀값>"

    async def test_overlap_through_the_notifier(self, env: Env) -> None:
        await env.notifier(redact=("abcdefgh", "efghijkl")).send("x abcdefghijkl y")
        assert env.body()["content"] == "x <비밀값> y"
        assert env.logs == ["[ops] x <비밀값> y"]


class TestNotifierConstructorChecks:
    def test_trust_env_client_is_rejected(self) -> None:
        client = httpx2.AsyncClient(trust_env=True)
        with pytest.raises(ValueError, match="trust_env") as info:
            DiscordNotifier(
                token="tok-abcdefgh", channel_id=CHANNEL, admin_user_id=ADMIN, client=client
            )
        assert "tok-abcdefgh" not in str(info.value)

    def test_trust_env_false_client_is_accepted(self) -> None:
        DiscordNotifier(token="t", channel_id=CHANNEL, admin_user_id=ADMIN, client=idle_client())


class _SlowDrip(httpx2.AsyncByteStream):
    def __init__(self) -> None:
        self.closed = False

    async def __aiter__(self) -> Any:
        while True:
            await asyncio.sleep(0.1)
            yield b" "

    async def aclose(self) -> None:
        self.closed = True


class TestNotifierTimeBounds:
    def test_constants(self) -> None:
        assert notify_module._REQUEST_TIMEOUT_SECONDS == 10.0
        assert notify_module._TOTAL_TIMEOUT_SECONDS == 15.0

    async def test_hang_before_headers_is_bounded_and_logged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(notify_module, "_TOTAL_TIMEOUT_SECONDS", 0.3)

        async def handler(request: httpx2.Request) -> httpx2.Response:
            await asyncio.sleep(30)
            return httpx2.Response(200)

        e = Env(handler)
        try:
            started = time.monotonic()
            with hang_guard(5):
                await e.notifier().send("x")
        finally:
            await e.client.aclose()
        assert time.monotonic() - started < 3
        assert e.logs == ["[ops] x", "[ops] 운영 채널 전송 실패: TimeoutError"]

    async def test_slow_drip_body_is_never_read_and_is_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(notify_module, "_TOTAL_TIMEOUT_SECONDS", 0.4)
        stream = _SlowDrip()
        e = Env(lambda request: httpx2.Response(200, stream=stream))
        try:
            started = time.monotonic()
            with hang_guard(5):
                await e.notifier().send("x")
        finally:
            await e.client.aclose()
        assert time.monotonic() - started < 0.3
        assert e.logs == ["[ops] x"]
        assert stream.closed

    async def test_slow_drip_with_error_status_is_not_read_either(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(notify_module, "_TOTAL_TIMEOUT_SECONDS", 0.4)
        stream = _SlowDrip()
        e = Env(lambda request: httpx2.Response(500, stream=stream))
        try:
            with hang_guard(5):
                await e.notifier().send("x")
        finally:
            await e.client.aclose()
        assert e.logs[-1] == "[ops] 운영 채널 전송 실패: HTTP 500"
        assert stream.closed
