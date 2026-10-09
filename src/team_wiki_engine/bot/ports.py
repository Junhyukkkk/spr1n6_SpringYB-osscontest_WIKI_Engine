"""디스코드와 무관한 경계 타입. 디스코드 어댑터(`gateway.py`)와 질문 처리가 이것으로만 만난다.

`QuestionContext`·`AnswerThread`의 속성은 읽기 전용(`@property`)이다. 쓰기 가능한 변수로 두면
frozen 데이터클래스 구현을 `mypy --strict`가 거부한다.
"""

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol


class AnswerThread(Protocol):
    @property
    def thread_id(self) -> str: ...

    async def post(self, chunks: Sequence[str]) -> None: ...

    def typing(self) -> AbstractAsyncContextManager[None]: ...


class QuestionContext(Protocol):
    @property
    def user_id(self) -> str: ...

    @property
    def question(self) -> str: ...

    @property
    def existing_thread_id(self) -> str | None: ...

    async def open_thread(self, name: str) -> AnswerThread: ...

    async def reply_in_place(self, text: str) -> None: ...


@dataclass(frozen=True)
class IncomingMessage:
    """디스코드 메시지에서 필요한 것만 옮긴 값. 모든 필드는 믿을 수 없는 입력이다."""

    message_id: str
    author_id: str
    author_is_bot: bool
    is_system: bool
    guild_id: str | None
    channel_id: str
    parent_channel_id: str | None
    content: str
    has_attachments: bool


class MessageReplies(Protocol):
    async def reply_in_place(self, text: str) -> None: ...

    async def open_thread(self, name: str) -> AnswerThread: ...


class GatewayHandler(Protocol):
    async def on_ready(self, bot_user_id: str) -> None: ...

    async def on_message(self, message: IncomingMessage, replies: MessageReplies) -> None: ...


class GatewayConfigError(Exception):
    """설정 문제(메시지는 운영 채널에 그대로 써도 안전하다)."""


class Gateway(Protocol):
    async def run(self, handler: GatewayHandler) -> None:
        """연결하고 메시지를 `handler`로 보낸다. 닫히면 돌아온다."""
        ...

    async def close(self) -> None: ...
