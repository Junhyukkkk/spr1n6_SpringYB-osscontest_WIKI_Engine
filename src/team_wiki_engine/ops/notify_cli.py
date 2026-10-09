"""`team-wiki-notify <유닛>` — systemd 유닛 실패를 운영 채널에 알린다.

시간대 자료가 빠진 서버에서도 실패 알림이 나가야 하므로 `util.time`·`github.client`·
`metrics.record`를 (간접으로도) import하지 않는다. `load_secrets`도 부르지 않는다(Claude 키 불필요).
전송 실패도 종료 코드 0이다 — `DiscordNotifier`가 로그에 남기고, 이 유닛에는 `OnFailure`가 없다.
"""

import asyncio
import socket
import sys
from collections.abc import Callable, Mapping, Sequence

import httpx2

from team_wiki_engine.config.load import ConfigError, load_config
from team_wiki_engine.ops.notify import DiscordNotifier, unit_failure_message

USAGE = "사용법: team-wiki-notify <실패한 유닛 이름>"
_DEFAULT_CONFIG = "/etc/team-wiki/team.json"
_SECRET_NAMES = ("ANTHROPIC_API_KEY", "DISCORD_TOKEN", "GITHUB_TOKEN")


def _default_client() -> httpx2.AsyncClient:
    return httpx2.AsyncClient(timeout=httpx2.Timeout(15.0), follow_redirects=False, trust_env=False)


async def amain(
    argv: Sequence[str],
    env: Mapping[str, str],
    *,
    http_factory: Callable[[], httpx2.AsyncClient] | None = None,
    hostname: Callable[[], str] = socket.gethostname,
) -> int:
    if len(argv) != 1 or argv[0].startswith("-"):
        print(USAGE, file=sys.stderr)
        return 2
    try:
        config = load_config(env.get("TEAM_WIKI_CONFIG") or _DEFAULT_CONFIG)
    except ConfigError as e:
        print(str(e), file=sys.stderr)
        return 2
    token = env.get("DISCORD_TOKEN", "").strip() or None
    redact = [v for name in _SECRET_NAMES if (v := env.get(name, "").strip())]
    try:
        host = hostname()
    except Exception:
        host = ""
    async with (http_factory or _default_client)() as http:
        notifier = DiscordNotifier(
            token=token,
            channel_id=config.discord.ops_channel_id,
            admin_user_id=config.discord.admin_user_id,
            client=http,
            redact=redact,
        )
        await notifier.send(unit_failure_message(argv[0], host), mention_admin=True)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    import os

    args = sys.argv[1:] if argv is None else argv
    return asyncio.run(amain(args, os.environ))


if __name__ == "__main__":
    raise SystemExit(main())
