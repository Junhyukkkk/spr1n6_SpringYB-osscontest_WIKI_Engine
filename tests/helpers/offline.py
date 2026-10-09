"""테스트 오프라인 안전장치의 기록과 판정(`tests/conftest.py`가 쓴다).

안전장치가 막는 것: AF_INET/AF_INET6 소켓의 `connect`·`connect_ex`, 로컬이 아닌 호스트의
이름 조회(`getaddrinfo`·`gethostbyname`·`gethostbyname_ex`·`gethostbyaddr`·`getnameinfo`),
`claude_agent_sdk.query`·`team_wiki_engine.claude.runner.query`, SDK의 CLI 전송
(`SubprocessCLITransport.connect`).

막지 못하는 것(뒤 태스크가 알아야 하는 한계):
- UDP `sendto`·`sendmsg`(연결 없이 보내는 소켓), 원시 `_socket` 직접 사용
- 안전장치보다 먼저 `from socket import …`로 가져간 이름(패치가 닿지 않는다)
- 임의의 하위 프로세스(`curl` 등) — git의 https 전송은 `GIT_ALLOW_PROTOCOL=file`로 따로 막는다
- 클래스·모듈·세션 범위 fixture와 import 시점 코드(함수 범위 autouse라 그 전에 돈다)
"""

import os
from dataclasses import dataclass, field

import pytest


@dataclass
class OfflineGuard:
    """막은 시도의 기록. 값은 짧은 설명이고 비밀(프롬프트·토큰)은 담지 않는다."""

    attempts: list[str] = field(default_factory=list)


def live_enabled() -> bool:
    """실제 Claude를 부르도록 켰는가. `tests/claude/test_live_smoke.py`와 같은 규칙이다."""
    has_key = os.environ.get("ANTHROPIC_API_KEY", "").strip() != ""
    return os.environ.get("LIVE_CLAUDE") == "1" and has_key


def is_live_test(item: pytest.Item) -> bool:
    """안전장치를 빼도 되는 테스트인가: `live` 마커가 있고 실제 실행이 켜져 있을 때만 참이다.

    켜져 있지 않으면 `live` 테스트는 skip되거나(스모크) 그대로 막힌 채 돈다.
    """
    return item.get_closest_marker("live") is not None and live_enabled()
