"""디스코드 어댑터(`discord.py`). 저장소에서 `discord`를 import하는 유일한 `src` 모듈이다.

디스코드 메시지를 디스코드와 무관한 `IncomingMessage`로 바꾸고, 보내기는 항상 멘션 없음으로
감싼다(클라이언트 기본값, 보낼 때마다 `allowed_mentions`, 답장은 `mention_author=False`).
로그에는 예외 종류 이름만 싣는다 — 디스코드 오류 메시지에 질문·답·토큰이 섞일 수 있다.
"""

import logging
import sys
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Final

import discord

from team_wiki_engine.bot.ports import (
    AnswerThread,
    Gateway,
    GatewayConfigError,
    GatewayHandler,
    IncomingMessage,
)
from team_wiki_engine.bot.text import is_snowflake

log = logging.getLogger(__name__)

NO_MENTIONS: Final = discord.AllowedMentions.none()
THREAD_TYPES: Final = frozenset(
    {
        discord.ChannelType.public_thread,
        discord.ChannelType.private_thread,
        discord.ChannelType.news_thread,
    }
)
AUTO_ARCHIVE_MINUTES: Final = 1440
ANSWERABLE_TYPES: Final = frozenset({discord.MessageType.default, discord.MessageType.reply})


def build_intents() -> discord.Intents:
    intents = discord.Intents.none()
    intents.guilds = True
    intents.guild_messages = True
    intents.message_content = True
    return intents


class _BotClient(discord.Client):
    def __init__(self, handler: GatewayHandler) -> None:
        super().__init__(
            intents=build_intents(),
            allowed_mentions=NO_MENTIONS,
            max_messages=None,
            member_cache_flags=discord.MemberCacheFlags.none(),
            chunk_guilds_at_startup=False,
        )
        self._handler = handler

    async def on_ready(self) -> None:
        user = self.user
        if user is None:
            return
        await self._handler.on_ready(str(user.id))

    async def on_message(self, message: discord.Message) -> None:
        incoming = to_incoming(message)
        if incoming is not None:
            await self._handler.on_message(incoming, DiscordReplies(message))

    async def on_error(self, event_method: str, /, *args: Any, **kwargs: Any) -> None:
        kind = sys.exc_info()[0]
        log.error(
            "디스코드 이벤트 처리 중 오류: %s (%s)",
            event_method,
            kind.__name__ if kind is not None else "알 수 없음",
        )


def build_client(handler: GatewayHandler) -> discord.Client:
    if hasattr(discord.VoiceClient, "warn_nacl"):
        discord.VoiceClient.warn_nacl = False  # 음성은 쓰지 않는다
    return _BotClient(handler)


def to_incoming(message: discord.Message) -> IncomingMessage | None:
    """예외를 던지지 않는다. 읽을 수 없거나 ID 형식이 아니면 `None`."""
    try:
        message_id = str(message.id)
        author_id = str(message.author.id)
        channel = message.channel
        channel_id = str(channel.id)
        parent_channel_id: str | None = None
        if channel.type in THREAD_TYPES:
            parent = getattr(channel, "parent_id", None)
            if isinstance(parent, int) and not isinstance(parent, bool):
                parent_channel_id = str(parent)
        guild_id = str(message.guild.id) if message.guild else None
        content = message.content if isinstance(message.content, str) else ""
        incoming = IncomingMessage(
            message_id=message_id,
            author_id=author_id,
            author_is_bot=bool(message.author.bot),
            is_system=message.webhook_id is not None or message.type not in ANSWERABLE_TYPES,
            guild_id=guild_id,
            channel_id=channel_id,
            parent_channel_id=parent_channel_id,
            content=content,
            has_attachments=bool(message.attachments),
        )
    except Exception:
        return None
    ids = [incoming.message_id, incoming.author_id, incoming.channel_id]
    ids += [i for i in (incoming.guild_id, incoming.parent_channel_id) if i is not None]
    if not all(is_snowflake(i) for i in ids):
        return None
    return incoming


class DiscordThread:
    def __init__(self, thread: discord.Thread) -> None:
        self._thread = thread

    @property
    def thread_id(self) -> str:
        return str(self._thread.id)

    async def post(self, chunks: Sequence[str]) -> None:
        for chunk in chunks:
            await self._thread.send(content=chunk, allowed_mentions=NO_MENTIONS)

    def typing(self) -> AbstractAsyncContextManager[None]:
        return self._typing()

    @asynccontextmanager
    async def _typing(self) -> AsyncIterator[None]:
        cm = self._thread.typing()
        entered = False
        try:
            await cm.__aenter__()
            entered = True
        except discord.HTTPException as exc:  # 타이핑 실패가 답을 막지 않는다
            log.warning("타이핑 표시를 보내지 못했습니다 (%s)", type(exc).__name__)
        try:
            yield
        finally:
            if entered:
                try:
                    await cm.__aexit__(None, None, None)
                except discord.HTTPException as exc:
                    log.warning("타이핑 표시를 끝내지 못했습니다 (%s)", type(exc).__name__)


class DiscordReplies:
    def __init__(self, message: discord.Message) -> None:
        self._message = message

    async def reply_in_place(self, text: str) -> None:
        await self._message.reply(content=text, mention_author=False, allowed_mentions=NO_MENTIONS)

    async def open_thread(self, name: str) -> AnswerThread:
        channel = self._message.channel
        if isinstance(channel, discord.Thread):
            return DiscordThread(channel)
        thread = await self._message.create_thread(
            name=name, auto_archive_duration=AUTO_ARCHIVE_MINUTES
        )
        return DiscordThread(thread)


class DiscordGateway:
    def __init__(
        self,
        token: str,
        *,
        client_factory: Callable[[GatewayHandler], discord.Client] = build_client,
    ) -> None:
        self._token = token
        self._factory = client_factory
        self._client: discord.Client | None = None
        self._closed = False

    def __repr__(self) -> str:
        return f"DiscordGateway(connected={self._client is not None})"

    async def run(self, handler: GatewayHandler) -> None:
        if self._closed:
            return
        client = self._factory(handler)
        self._client = client
        try:
            await client.start(self._token)
        except discord.LoginFailure:
            raise GatewayConfigError("디스코드 봇 토큰이 거부되었습니다") from None
        except discord.PrivilegedIntentsRequired:
            raise GatewayConfigError(
                "디스코드 개발자 포털에서 Message Content Intent를 켜야 합니다"
            ) from None

    async def close(self) -> None:
        self._closed = True
        if self._client is not None:
            await self._client.close()


_: type[Gateway] = DiscordGateway


def configure_discord_logging() -> None:
    logger = logging.getLogger("discord")
    if logger.level < logging.INFO:
        logger.setLevel(logging.INFO)
