"""디스코드 어댑터. 실제 연결은 하지 않는다(`login`·`connect`·`start`는 부르지 않는다)."""

import logging
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, NonCallableMagicMock

import discord
import pytest

from team_wiki_engine.bot.gateway import (
    NO_MENTIONS,
    DiscordGateway,
    DiscordReplies,
    DiscordThread,
    build_client,
    build_intents,
    configure_discord_logging,
    to_incoming,
)
from team_wiki_engine.bot.ports import GatewayConfigError, GatewayHandler
from tests.helpers.offline import OfflineGuard

TOKEN = "tok-123456789"


class NullHandler:
    async def on_ready(self, bot_user_id: str) -> None:
        self.ready = bot_user_id

    async def on_message(self, message: Any, replies: Any) -> None:
        self.message = (message, replies)


def make_message(**over: Any) -> NonCallableMagicMock:
    m = NonCallableMagicMock(spec=discord.Message)
    m.id = 1000
    m.author = MagicMock()
    m.author.id = 500
    m.author.bot = False
    m.webhook_id = None
    m.type = discord.MessageType.default
    m.guild = MagicMock()
    m.guild.id = 100
    m.channel = MagicMock()
    m.channel.id = 200
    m.channel.type = discord.ChannelType.text
    m.content = "<@999> 질문"
    m.attachments = []
    for key, value in over.items():
        setattr(m, key, value)
    return m


def test_intents() -> None:
    i = build_intents()
    assert i.guilds and i.guild_messages and i.message_content
    assert not i.members and not i.presences


def test_build_client(offline_guard: OfflineGuard) -> None:
    client = build_client(NullHandler())
    assert isinstance(client, discord.Client)
    mentions = client.allowed_mentions
    assert mentions is not None
    assert not (mentions.everyone or mentions.users or mentions.roles or mentions.replied_user)
    assert client._connection.max_messages is None
    assert offline_guard.attempts == []


class TestToIncoming:
    def test_plain_channel(self) -> None:
        m = to_incoming(make_message())
        assert m is not None
        assert (m.message_id, m.author_id, m.guild_id, m.channel_id) == (
            "1000",
            "500",
            "100",
            "200",
        )
        assert m.parent_channel_id is None and not m.is_system and not m.has_attachments

    def test_thread_has_parent(self) -> None:
        channel = MagicMock()
        channel.id = 900
        channel.type = discord.ChannelType.public_thread
        channel.parent_id = 200
        m = to_incoming(make_message(channel=channel))
        assert m is not None and m.parent_channel_id == "200" and m.channel_id == "900"

    def test_system_variants(self) -> None:
        hook = to_incoming(make_message(webhook_id=5))
        pin = to_incoming(make_message(type=discord.MessageType.pins_add))
        assert hook is not None and hook.is_system
        assert pin is not None and pin.is_system

    def test_no_guild(self) -> None:
        m = to_incoming(make_message(guild=None))
        assert m is not None and m.guild_id is None

    def test_bad_id(self) -> None:
        author = MagicMock()
        author.id = -1
        author.bot = False
        assert to_incoming(make_message(author=author)) is None

    @pytest.mark.parametrize("content", [None, MagicMock()])
    def test_odd_content(self, content: object) -> None:
        m = to_incoming(make_message(content=content))
        assert m is not None and m.content == ""

    def test_attribute_error_returns_none(self) -> None:
        class Broken:
            @property
            def id(self) -> int:
                raise AttributeError("x")

        assert to_incoming(make_message(channel=Broken())) is None


def make_thread() -> NonCallableMagicMock:
    t = NonCallableMagicMock(spec=discord.Thread)
    t.id = 900
    t.send = AsyncMock()
    t.type = discord.ChannelType.public_thread
    return t


async def test_thread_post_in_order_without_mentions() -> None:
    thread = make_thread()
    await DiscordThread(thread).post(["@everyone <@&777> 안녕", "b"])
    calls = thread.send.await_args_list
    assert [c.kwargs["content"] for c in calls] == ["@everyone <@&777> 안녕", "b"]
    assert all(c.kwargs["allowed_mentions"] is NO_MENTIONS for c in calls)
    assert DiscordThread(thread).thread_id == "900"


async def test_typing_failure_is_swallowed(caplog: pytest.LogCaptureFixture) -> None:
    thread = make_thread()
    response = MagicMock()
    response.status = 403
    response.reason = "Forbidden"
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(side_effect=discord.Forbidden(response, "ghp_secret"))
    cm.__aexit__ = AsyncMock(return_value=False)
    thread.typing = MagicMock(return_value=cm)
    ran = False
    with caplog.at_level(logging.DEBUG):
        async with DiscordThread(thread).typing():
            ran = True
    assert ran and "ghp_secret" not in caplog.text and "Forbidden" in caplog.text


async def test_reply_in_place_and_open_thread() -> None:
    message = make_message()
    message.reply = AsyncMock()
    new_thread = make_thread()
    message.create_thread = AsyncMock(return_value=new_thread)
    replies = DiscordReplies(message)
    await replies.reply_in_place("안내")
    kwargs = message.reply.await_args.kwargs
    assert kwargs["mention_author"] is False and kwargs["allowed_mentions"] is NO_MENTIONS
    opened = await replies.open_thread("이름")
    message.create_thread.assert_awaited_once_with(name="이름", auto_archive_duration=1440)
    assert opened.thread_id == "900"

    inside = make_thread()
    message2 = make_message(channel=inside)
    message2.create_thread = AsyncMock()
    same = await DiscordReplies(message2).open_thread("x")
    message2.create_thread.assert_not_awaited()
    assert same.thread_id == "900"


class FakeClient:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.closed = 0

    async def start(self, token: str) -> None:
        if self.error is not None:
            raise self.error

    async def close(self) -> None:
        self.closed += 1


def gateway(client: FakeClient) -> tuple[DiscordGateway, list[int]]:
    made: list[int] = []

    def factory(handler: GatewayHandler) -> Any:
        made.append(1)
        return client

    return DiscordGateway(TOKEN, client_factory=factory), made


async def test_gateway_login_failure_hides_token() -> None:
    gw, _ = gateway(FakeClient(discord.LoginFailure("Improper token " + TOKEN)))
    with pytest.raises(GatewayConfigError) as info:
        await gw.run(NullHandler())
    assert TOKEN not in str(info.value) and info.value.__cause__ is None
    assert info.value.__suppress_context__ is True


async def test_gateway_intent_and_other_errors() -> None:
    gw, _ = gateway(FakeClient(discord.PrivilegedIntentsRequired(shard_id=None)))
    with pytest.raises(GatewayConfigError, match="Message Content Intent"):
        await gw.run(NullHandler())
    gw2, _ = gateway(FakeClient(RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="boom"):
        await gw2.run(NullHandler())


async def test_gateway_close_semantics() -> None:
    client = FakeClient()
    gw, made = gateway(client)
    await gw.close()
    await gw.close()
    await gw.run(NullHandler())  # 연결하지 않고 돌아온다
    assert made == []
    assert TOKEN not in repr(gw) and repr(gw) == "DiscordGateway(connected=False)"


async def test_gateway_close_after_run_is_repeatable() -> None:
    client = FakeClient()
    gw, made = gateway(client)
    await gw.run(NullHandler())
    assert made == [1]
    await gw.close()
    await gw.close()
    assert client.closed >= 1


async def test_on_error_logs_kind_only(caplog: pytest.LogCaptureFixture) -> None:
    client = build_client(NullHandler())
    with caplog.at_level(logging.DEBUG):
        try:
            raise ValueError("ghp_secret")
        except ValueError:
            assert sys.exc_info()[0] is ValueError
            await client.on_error("on_message")
    assert "ghp_secret" not in caplog.text and "ValueError" in caplog.text
    assert "Traceback" not in caplog.text


def test_configure_logging() -> None:
    logger = logging.getLogger("discord")
    old = logger.level
    try:
        logger.setLevel(logging.DEBUG)
        configure_discord_logging()
        assert logger.level == logging.INFO
        logger.setLevel(logging.WARNING)
        configure_discord_logging()
        assert logger.level == logging.WARNING
    finally:
        logger.setLevel(old)
