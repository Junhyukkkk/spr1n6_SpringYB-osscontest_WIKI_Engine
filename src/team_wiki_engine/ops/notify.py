"""운영 채널 알림(Discord REST).

알림은 실패해도 동기화를 멈추지 않는다. 항상 로그(기본 표준 출력)에 먼저 남기고, 토큰과 채널이
올바를 때만 Discord로 보낸다. 토큰·알려진 비밀값은 로그와 본문 모두에서 지우고, 전송 실패는
상태 코드나 예외 클래스 이름만 남긴다(예외 메시지·응답 본문은 싣지 않는다).
"""

import asyncio
import json
import re
from collections.abc import Callable, Iterable, Sequence
from typing import Final, Protocol

import httpx2

DISCORD_API = "https://discord.com/api/v10"
DISCORD_LIMIT = 2000

_REQUEST_TIMEOUT_SECONDS = 10.0  # httpx2 연결·읽기·쓰기·풀 시간 제한
_TOTAL_TIMEOUT_SECONDS = 15.0  # 한 번의 전송 전체 상한(응답이 천천히 흘러도 끊는다)
_MIN_SECRET_LENGTH = 8
_SECRET_PLACEHOLDER = "<비밀값>"
_USER_AGENT = "team-wiki-engine"
_SNOWFLAKE = re.compile(r"[0-9]{1,25}")
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


class Notifier(Protocol):
    async def send(self, text: str, *, mention_admin: bool = False) -> None: ...


def _secret_list(secrets: Iterable[str]) -> list[str]:
    if isinstance(secrets, (str, bytes, bytearray)):
        # 문자열 하나를 넘기면 글자 단위로 쪼개져 모두 무시되고 비밀값이 새어 나간다.
        raise TypeError("비밀값은 문자열 목록으로 넘겨야 합니다")
    values = list(secrets)
    if not all(isinstance(v, str) for v in values):
        raise TypeError("비밀값은 문자열이어야 합니다")
    return values


def _spans(text: str, secret: str) -> list[list[int]]:
    """`secret`이 나오는 모든 자리(겹침 포함)를 이어 붙인 `[시작, 끝)` 구간 목록."""
    spans: list[list[int]] = []
    start = text.find(secret)
    while start != -1:
        end = start + len(secret)
        if spans and start <= spans[-1][1]:
            spans[-1][1] = end
        else:
            spans.append([start, end])
        start = text.find(secret, start + 1)
    return spans


def redact_secrets(text: str, secrets: Iterable[str]) -> str:
    """`secrets`에 든 값(8자 이상)을 모두 `<비밀값>`으로 바꾼다.

    모든 비밀값의 맞는 자리를 구한 뒤 겹치거나 이어진 구간은 하나로 합쳐 한 번에 치환한다
    (겹치는 비밀값의 조각이 남지 않는다). `str`·`bytes` 하나를 넘기면 `TypeError`다.
    """
    values = {s for s in _secret_list(secrets) if len(s) >= _MIN_SECRET_LENGTH}
    spans = sorted(span for value in values for span in _spans(text, value))
    if not spans:
        return text
    merged: list[list[int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    pieces: list[str] = []
    cursor = 0
    for start, end in merged:
        pieces.append(text[cursor:start])
        pieces.append(_SECRET_PLACEHOLDER)
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def _utf16_units(char: str) -> int:
    return 2 if ord(char) > 0xFFFF else 1


def truncate_utf16(text: str, limit: int) -> str:
    """UTF-16 코드 유닛 `limit`개 안으로 자른다. 서로게이트 쌍은 끊지 않는다."""
    if limit <= 0:
        return ""
    used = 0
    end = 0
    # 코드 포인트 수 ≤ 유닛 수이므로 앞 `limit`개만 보면 충분하다.
    for char in text[:limit]:
        units = _utf16_units(char)
        if used + units > limit:
            break
        used += units
        end += 1
    return text[:end]


def _utf16_length(text: str) -> int:
    return sum(_utf16_units(c) for c in text)


def _print_log(line: str) -> None:
    print(line, flush=True)


class DiscordNotifier:
    """Discord 채널로 운영 알림을 보낸다. `send`는 `CancelledError` 외에는 던지지 않는다."""

    def __init__(
        self,
        *,
        token: str | None,
        channel_id: str,
        admin_user_id: str,
        client: httpx2.AsyncClient | None,
        log: Callable[[str], None] | None = None,
        redact: Sequence[str] = (),
    ) -> None:
        if client is not None and client.trust_env:
            # 환경 변수의 프록시·인증 설정이 요청에 끼어들지 않게 한다(배선: `trust_env=False`).
            raise ValueError("httpx2 클라이언트는 trust_env=False로 만들어야 합니다")
        self.__token = token
        self.__channel_id = channel_id
        self.__admin_user_id = admin_user_id
        self.__client = client
        self.__log = log if log is not None else _print_log
        self.__secrets = tuple(s for s in (token, *_secret_list(redact)) if s)

    def __repr__(self) -> str:
        return f"DiscordNotifier(configured={self.__can_send()})"

    def __can_send(self) -> bool:
        return (
            self.__client is not None
            and bool(self.__token)
            and _SNOWFLAKE.fullmatch(self.__channel_id) is not None
        )

    def __emit(self, line: str) -> None:
        try:
            self.__log(line)
        except Exception:  # 로그 실패가 동기화를 멈추지 않는다
            pass

    async def send(self, text: str, *, mention_admin: bool = False) -> None:
        mention = mention_admin and _SNOWFLAKE.fullmatch(self.__admin_user_id) is not None
        suffix = f" <@{self.__admin_user_id}>" if mention else ""
        clean = _LONE_SURROGATE.sub("�", redact_secrets(text, self.__secrets))
        # 멘션이 잘려 나가지 않도록 본문 쪽을 줄인다.
        content = truncate_utf16(clean, DISCORD_LIMIT - _utf16_length(suffix)) + suffix
        self.__emit(f"[ops] {content}")
        client = self.__client
        if client is None or not self.__can_send():
            return
        payload = {
            "content": content,
            "allowed_mentions": {"parse": [], "users": [self.__admin_user_id] if mention else []},
        }
        try:
            async with asyncio.timeout(_TOTAL_TIMEOUT_SECONDS):
                async with client.stream(
                    "POST",
                    f"{DISCORD_API}/channels/{self.__channel_id}/messages",
                    content=json.dumps(payload).encode("utf-8"),
                    headers={
                        "Authorization": f"Bot {self.__token}",
                        "User-Agent": _USER_AGENT,
                        "Content-Type": "application/json",
                    },
                    follow_redirects=False,
                    timeout=_REQUEST_TIMEOUT_SECONDS,
                ) as response:
                    status = response.status_code
        except Exception as exc:  # CancelledError는 BaseException이라 지나간다
            self.__emit(f"[ops] 운영 채널 전송 실패: {type(exc).__name__}")
            return
        if not 200 <= status < 300:
            self.__emit(f"[ops] 운영 채널 전송 실패: HTTP {status}")


UNIT_NAME_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9@._:\-]{1,200}")
HOST_NAME_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9.\-]{1,64}")


def unit_failure_message(unit: str, host: str) -> str:
    """systemd 유닛 실패 알림 문구. 이름·호스트는 정규식에 맞을 때만 싣는다(멘션·형식 위조 방지)."""
    name = unit.strip()
    shown_host = host.strip()
    if HOST_NAME_RE.fullmatch(shown_host) is None:
        shown_host = "(알 수 없는 호스트)"
    # 정규식은 인스턴스 유닛(`a@b.service`)을 위해 `@`를 허용하므로, 멘션 글자는 따로 거른다.
    if (
        UNIT_NAME_RE.fullmatch(name) is not None
        and not name.startswith("@")
        and "@everyone" not in name
        and "@here" not in name
    ):
        return (
            f"[systemd] {name} 유닛이 실패했습니다 ({shown_host}). "
            f"확인: journalctl -u {name} -n 100"
        )
    return f"[systemd] (알 수 없음) 유닛이 실패했습니다 ({shown_host}). 확인: systemctl --failed"
