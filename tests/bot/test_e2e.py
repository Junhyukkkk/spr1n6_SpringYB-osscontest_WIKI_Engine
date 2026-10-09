"""봇 끝까지 가는 테스트: 가짜 게이트웨이·가짜 Claude, 실제 git, `delivery: local`, 네트워크 0."""

import asyncio
import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2

from team_wiki_engine.bot.main import amain
from team_wiki_engine.bot.replies import (
    CODE_SOURCE_NOTE,
    PAUSE_ACK_TEXT,
    PAUSED_TEXT,
    RESUME_ACK_TEXT,
    rejection_text,
)
from team_wiki_engine.bot.wiring import build_bot_deps
from team_wiki_engine.claude.types import RunClaudeInput, ToolCallTrace
from team_wiki_engine.metrics.record import (
    STATE_FILES,
    GapRecord,
    QuestionRecord,
    read_jsonl,
)
from team_wiki_engine.sync.lock import acquire_lock
from tests.helpers.bot import FakeGateway, FakeReplies, FakeThread, incoming, wait_until
from tests.helpers.git import seed_bare
from tests.helpers.http import mock_client
from tests.helpers.offline import OfflineGuard
from tests.helpers.runner import ScriptedRunner
from tests.helpers.sync_env import ENGINE_DIR

BASELINE = (
    '{"repos": {"worker": {"commit": "' + "a" * 40 + '", "archived": false}}, '
    '"raw_processed": [], "updated_at": "2026-10-07T00:00:00Z"}'
)


async def test_bot_end_to_end(tmp_dir: Path, offline_guard: OfflineGuard) -> None:
    async with asyncio.timeout(60):
        root = tmp_dir
        home = root / "home"
        data_bare = root / "data.git"
        await seed_bare(
            data_bare, root / "data-work", {"baseline.json": BASELINE, "wiki/index.md": "# 목차\n"}
        )
        config_path = root / "team.json"
        config_path.write_text(
            json.dumps(
                {
                    "repos": [{"name": "worker", "url": "https://github.com/o/worker.git"}],
                    "dataRepo": {"url": str(data_bare)},
                    "discord": {
                        "guildId": "100",
                        "channelIds": ["200"],
                        "opsChannelId": "",
                        "adminUserId": "400",
                    },
                }
            ),
            encoding="utf-8",
        )
        (root / "git-home").mkdir()
        env = {
            "TEAM_WIKI_HOME": str(home),
            "TEAM_WIKI_CONFIG": str(config_path),
            "TEAM_WIKI_ENGINE_DIR": str(ENGINE_DIR),
            "ANTHROPIC_API_KEY": "test-key-not-real",
            "DISCORD_TOKEN": "test-discord-token-not-real",
            "PATH": os.environ["PATH"],
            "HOME": str(root / "git-home"),
        }
        repos = home / "repos"
        (repos / "worker/src").mkdir(parents=True)
        (repos / "worker/src/A.kt").write_text("class A", encoding="utf-8")

        def script(inp: RunClaudeInput, call: int) -> Mapping[str, Any]:
            if call == 0:
                return {
                    "text": "worker는 메시지를 읽어요\n— 근거: wiki/index.md",
                    "tool_calls": [ToolCallTrace("Read", home / "data/wiki/index.md", True)],
                }
            if call == 1:
                return {"text": "traceId는 worker가 넣어요\n— 근거: wiki/index.md"}
            if call == 2:
                return {
                    "text": "A에는 메서드가 있어요\n— 근거: worker/src/A.kt",
                    "tool_calls": [ToolCallTrace("Read", repos / "worker/src/A.kt", True)],
                }
            return {"text": "@everyone 필드는 이렇습니다\n— 근거: wiki/index.md"}

        runner = ScriptedRunner(script)
        requests: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            requests.append(request)
            return httpx2.Response(204)

        def build(*args: Any, **kwargs: Any) -> Any:
            return build_bot_deps(*args, runner=runner, **kwargs)

        gateway = FakeGateway()
        stop = asyncio.Event()
        task = asyncio.create_task(
            amain(
                [],
                env,
                gateway_factory=lambda token: gateway,
                http_factory=lambda: mock_client(handler),
                build_deps=build,
                stop=stop,
            )
        )
        await gateway.ready.wait()

        async def ask(replies: FakeReplies, wait_for_thread: bool = True, **fields: Any) -> None:
            await gateway.deliver(incoming(**fields), replies)
            if wait_for_thread:
                await wait_until(lambda: bool(replies.thread.posts or replies.replies))

        mention = "<@999> "
        r1 = FakeReplies(FakeThread("900"))
        await ask(
            r1, message_id="2001", author_id="501", content=mention + "worker는 어떤 메시지를 읽어?"
        )
        r2 = FakeReplies(FakeThread("900"))
        await ask(
            r2,
            message_id="2002",
            author_id="501",
            channel_id="900",
            parent_channel_id="200",
            content=mention + "그 필드 중 traceId는 누가 넣어?",
        )
        r3 = FakeReplies(FakeThread("901"))
        await ask(r3, message_id="2003", author_id="502", content=mention + "클래스 A 메서드?")
        r4 = FakeReplies(FakeThread("902"))
        await ask(
            r4,
            message_id="2004",
            author_id="503",
            content=mention + "@everyone 이 메시지 필드 알려줘",
        )
        calls_before = len(runner.inputs)
        r5 = FakeReplies()
        await ask(
            r5, wait_for_thread=False, message_id="2005", author_id="504", channel_id="300",
            content=mention + "질문",
        )  # fmt: skip
        await asyncio.sleep(0.2)
        assert r5.replies == [] and r5.thread.posts == [] and len(runner.inputs) == calls_before
        r6 = FakeReplies()
        await ask(r6, message_id="2006", author_id="501", content=mention + "세 번째")
        r7 = FakeReplies()
        await ask(r7, message_id="2007", author_id="400", content=mention + "!정지")
        r8 = FakeReplies()
        await ask(r8, message_id="2008", author_id="505", content=mention + "질문")
        calls_paused = len(runner.inputs)
        r9 = FakeReplies()
        await ask(r9, message_id="2009", author_id="400", content=mention + "!재개")
        stop.set()
        assert await task == 0

        # 스레드 답
        assert [len(r.thread.posts) for r in (r1, r2, r3, r4)] == [1, 1, 1, 1]
        assert runner.inputs[1].resume_session_id == "sess-1"
        assert r3.thread.posts[0][-1] == CODE_SOURCE_NOTE
        assert r4.thread.posts[0][0].startswith("@​everyone")
        assert "@everyone" not in "".join(r4.thread.posts[0])
        gaps = read_jsonl(home / "state" / STATE_FILES.gaps, GapRecord)
        assert len(gaps) == 1 and gaps[0].code_paths == ["worker/src/A.kt"]
        assert r6.replies == [rejection_text("user_rate")]
        assert r7.replies == [PAUSE_ACK_TEXT]
        assert r8.replies == [PAUSED_TEXT] and calls_paused == len(runner.inputs) == 4
        assert r9.replies == [RESUME_ACK_TEXT]

        # 기록
        records = read_jsonl(home / "state" / STATE_FILES.questions, QuestionRecord)
        assert len(records) == 4
        assert all(r.at.endswith("+09:00") and "501" != r.user for r in records)
        assert all(len(r.user) == 12 for r in records)
        raw = (home / "state" / STATE_FILES.questions).read_text(encoding="utf-8")
        assert "501" not in raw.replace(records[0].at, "")
        sessions = json.loads((home / "state" / STATE_FILES.sessions).read_text(encoding="utf-8"))
        assert {s["thread_id"] for s in sessions} == {"900", "901", "902"}
        control = json.loads((home / "state" / STATE_FILES.bot_control).read_text(encoding="utf-8"))
        assert control["paused"] is False
        assert requests == []
        lock = acquire_lock(home / "state" / STATE_FILES.data_lock, now=datetime.now(UTC))
        assert lock is not None
        lock.release()
        assert offline_guard.attempts == []
