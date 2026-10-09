import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx2
import pytest

from team_wiki_engine.ops.notify_cli import USAGE, amain
from tests.helpers.http import mock_client

CHANNEL = "123456789012345678"
ADMIN = "987654321098765432"
SENTINEL = "sk-ant-TESTSENTINEL0123456789"
GH = "ghp_TESTSENTINELGITHUB0123456789"


def write_config(tmp_path: Path) -> Path:
    path = tmp_path / "team.json"
    path.write_text(
        json.dumps(
            {
                "repos": [{"name": "server", "url": "https://github.com/o/server.git"}],
                "dataRepo": {"url": "/tmp/data.git"},
                "discord": {"opsChannelId": CHANNEL, "adminUserId": ADMIN},
            }
        ),
        encoding="utf-8",
    )
    return path


class Rig:
    def __init__(self, status: int = 200) -> None:
        self.requests: list[httpx2.Request] = []
        self.status = status

    def factory(self) -> httpx2.AsyncClient:
        return mock_client(self._handle)

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return httpx2.Response(self.status, json={})

    def body(self, i: int = 0) -> dict[str, Any]:
        loaded = json.loads(self.requests[i].content)
        assert isinstance(loaded, dict)
        return loaded


def env_for(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {"TEAM_WIKI_CONFIG": str(write_config(tmp_path)), **extra}


async def run(argv: list[str], env: dict[str, str], rig: Rig | None = None) -> int:
    rig = rig or Rig()
    return await amain(argv, env, http_factory=rig.factory, hostname=lambda: "h1")


@pytest.mark.parametrize("argv", [[], ["a", "b"], ["--help"], ["-x"]])
async def test_사용_오류(
    argv: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await run(argv, env_for(tmp_path, DISCORD_TOKEN="tok-12345678")) == 2
    assert USAGE in capsys.readouterr().err


async def test_설정_없음(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    env = {"TEAM_WIKI_CONFIG": str(tmp_path / "none.json")}
    assert await run(["u.service"], env) == 2
    assert capsys.readouterr().err.strip() != ""


async def test_정상(tmp_path: Path) -> None:
    rig = Rig()
    env = env_for(tmp_path, DISCORD_TOKEN="tok-12345678")
    assert await run(["team-wiki-sync.service"], env, rig) == 0
    assert len(rig.requests) == 1
    body = rig.body()
    assert "[systemd] team-wiki-sync.service 유닛이 실패했습니다" in body["content"]
    assert f"<@{ADMIN}>" in body["content"]
    assert body["allowed_mentions"] == {"parse": [], "users": [ADMIN]}


async def test_위조_이름(tmp_path: Path) -> None:
    rig = Rig()
    assert await run(["@everyone"], env_for(tmp_path, DISCORD_TOKEN="tok-12345678"), rig) == 0
    body = rig.body()
    assert "(알 수 없음)" in body["content"]
    assert "@everyone" not in body["content"]
    assert body["allowed_mentions"]["parse"] == []


async def test_디스코드_500은_0(tmp_path: Path) -> None:
    rig = Rig(500)
    assert await run(["u.service"], env_for(tmp_path, DISCORD_TOKEN="tok-12345678"), rig) == 0


async def test_토큰_없음(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rig = Rig()
    assert await run(["u.service"], env_for(tmp_path, DISCORD_TOKEN="  "), rig) == 0
    assert rig.requests == []
    assert "[ops] " in capsys.readouterr().out


async def test_묶지_않는다(tmp_path: Path) -> None:
    rig = Rig()
    env = env_for(tmp_path, DISCORD_TOKEN="tok-12345678")
    await run(["u.service"], env, rig)
    await run(["u.service"], env, rig)
    assert len(rig.requests) == 2


async def test_비밀값_비노출(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rig = Rig()
    env = env_for(
        tmp_path, DISCORD_TOKEN="tok-12345678", ANTHROPIC_API_KEY=SENTINEL, GITHUB_TOKEN=GH
    )
    await run([SENTINEL], env, rig)
    await run([GH], env, rig)
    out = capsys.readouterr()
    everything = out.out + out.err + b"".join(r.content for r in rig.requests).decode()
    assert SENTINEL not in everything
    assert GH not in everything


async def test_기본_클라이언트_설정(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class Recording(httpx2.AsyncClient):
        def __init__(self, **kwargs: Any) -> None:
            seen.update(kwargs)
            super().__init__(
                **{**kwargs, "transport": httpx2.MockTransport(lambda r: httpx2.Response(200))}
            )

    monkeypatch.setattr(httpx2, "AsyncClient", Recording)
    env = env_for(tmp_path, DISCORD_TOKEN="tok-12345678")
    assert await amain(["u.service"], env, hostname=lambda: "h1") == 0
    assert seen["trust_env"] is False
    assert seen["follow_redirects"] is False


def test_G15_시간대_모듈을_부르지_않는다() -> None:
    code = (
        "import sys, team_wiki_engine.ops.notify_cli, team_wiki_engine.ops.backup; "
        "print([m for m in ('team_wiki_engine.util.time', 'team_wiki_engine.github.client', "
        "'team_wiki_engine.metrics.record') if m in sys.modules])"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.stdout.strip() == "[]", result.stderr
