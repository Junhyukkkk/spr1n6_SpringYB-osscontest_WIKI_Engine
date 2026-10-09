"""봇 테스트 도우미: 질문·스레드 가짜와 닫힌 봇 환경. 실제 디스코드·Claude는 부르지 않는다."""

import asyncio
import os
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from team_wiki_engine.bot.answer import AnswerDeps, AnswerPipeline
from team_wiki_engine.bot.data_gate import DataGate
from team_wiki_engine.bot.limiter import Limiter
from team_wiki_engine.bot.ports import AnswerThread, GatewayHandler, IncomingMessage, MessageReplies
from team_wiki_engine.bot.sessions import SessionStore
from team_wiki_engine.claude.types import ClaudeRunner
from team_wiki_engine.config.load import Paths, parse_config, resolve_paths
from team_wiki_engine.config.schema import TeamConfig
from team_wiki_engine.git.client import GitClient
from team_wiki_engine.metrics.record import STATE_FILES, GapRecord, QuestionRecord, read_jsonl
from team_wiki_engine.sync.data_copy import prepare_data_copy
from team_wiki_engine.sync.repos import write_code_snapshot
from tests.helpers import write_tree
from tests.helpers.clock import ManualClock
from tests.helpers.git import seed_bare
from tests.helpers.notify import RecordingNotifier
from tests.helpers.runner import Script, ScriptedRunner

ENGINE_DIR = Path(__file__).resolve().parents[2]
USER_ID: Final = "5550123456789"
"""로그 누출 단언에 쓰는 눈에 띄는 ID(`"500"`은 시간·비용 숫자와 겹친다)."""

BASELINE_JSON: Final = (
    '{"repos": {"worker": {"commit": "' + "a" * 40 + '", "archived": false}}, '
    '"raw_processed": [], "updated_at": "2026-10-07T00:00:00Z"}'
)


class FakeThread:
    def __init__(self, thread_id: str = "900", fail_post: Exception | None = None) -> None:
        self.thread_id = thread_id
        self.fail_post = fail_post
        self.posts: list[list[str]] = []
        self.typing_entered = 0

    async def post(self, chunks: Sequence[str]) -> None:
        if self.fail_post is not None:
            raise self.fail_post
        self.posts.append(list(chunks))

    def typing(self) -> AbstractAsyncContextManager[None]:
        return self._typing()

    @asynccontextmanager
    async def _typing(self) -> AsyncIterator[None]:
        self.typing_entered += 1
        yield


@dataclass
class FakeQuestion:
    question: str
    user_id: str = USER_ID
    existing_thread_id: str | None = None
    open_error: Exception | None = None
    thread: FakeThread = field(default_factory=FakeThread)
    replies: list[str] = field(default_factory=list)
    opened_names: list[str] = field(default_factory=list)

    async def open_thread(self, name: str) -> AnswerThread:
        self.opened_names.append(name)
        if self.open_error is not None:
            raise self.open_error
        return self.thread

    async def reply_in_place(self, text: str) -> None:
        self.replies.append(text)


@dataclass
class BotEnv:
    root: Path
    paths: Paths
    config: TeamConfig
    runner: ScriptedRunner
    sessions: SessionStore
    limiter: Limiter
    gate: DataGate
    notifier: RecordingNotifier
    clock: ManualClock
    pipeline: AnswerPipeline

    def questions(self) -> list[QuestionRecord]:
        return read_jsonl(self.paths.state_dir / STATE_FILES.questions, QuestionRecord)

    def gaps(self) -> list[GapRecord]:
        return read_jsonl(self.paths.state_dir / STATE_FILES.gaps, GapRecord)


def _local_client(directory: Path, git_home: Path) -> GitClient:
    base_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(git_home)}
    return GitClient(directory, allowed_protocols="file", timeout_s=60.0, base_env=base_env)


async def make_bot_env(
    root: Path,
    *,
    script: Script | None = None,
    runner: ClaudeRunner | None = None,
    limits: Mapping[str, int] | None = None,
    redact: tuple[str, ...] = (),
) -> BotEnv:
    """`root`(비어 있는 임시 폴더) 안에 봇 환경을 만든다. `runner`를 주면 파이프라인이 쓴다."""
    git_home = root / "git-home"
    git_home.mkdir(parents=True, exist_ok=True)
    paths = resolve_paths({"TEAM_WIKI_HOME": str(root / "home")}, ENGINE_DIR)
    data_bare = root / "data.git"
    await seed_bare(
        data_bare,
        root / "data-work",
        {"baseline.json": BASELINE_JSON, "wiki/index.md": "# 목차\n"},
    )
    config = parse_config(
        {
            "repos": [{"name": "worker", "url": "https://github.com/o/worker.git"}],
            "dataRepo": {"url": str(data_bare)},
            "discord": {
                "guildId": "100",
                "channelIds": ["200"],
                "opsChannelId": "300",
                "adminUserId": "400",
            },
            "limits": dict(limits or {}),
        }
    )
    assert os.path.isabs(config.data_repo.url) and "://" not in config.data_repo.url
    paths.repos_dir.mkdir(parents=True, exist_ok=True)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    await prepare_data_copy(
        _local_client(paths.data_dir, git_home), config.data_repo.url, config.data_repo.branch
    )
    write_tree(paths.repos_dir, {"worker/src/A.kt": "class A"})
    write_code_snapshot(
        paths.state_dir, {"worker": "b" * 40}, datetime(2026, 10, 7, 0, 30, tzinfo=UTC)
    )
    clock = ManualClock(datetime(2026, 10, 7, 1, 0, tzinfo=UTC))
    scripted = ScriptedRunner(script)
    limiter = Limiter.from_limits(config.limits, now=clock.now, monotonic=clock.monotonic)
    sessions = SessionStore(paths.state_dir / STATE_FILES.sessions, now=clock.now)
    gate = DataGate(monotonic=clock.monotonic)
    notifier = RecordingNotifier()
    pipeline = AnswerPipeline(
        AnswerDeps(
            runner=runner if runner is not None else scripted,
            sessions=sessions,
            limiter=limiter,
            gate=gate,
            paths=paths,
            config=config,
            notifier=notifier,
            now=clock.now,
            monotonic=clock.monotonic,
            nonce=lambda: "0" * 12,
            redact=redact,
        )
    )
    return BotEnv(
        root=root,
        paths=paths,
        config=config,
        runner=scripted,
        sessions=sessions,
        limiter=limiter,
        gate=gate,
        notifier=notifier,
        clock=clock,
        pipeline=pipeline,
    )


class FakeReplies:
    """`MessageReplies` 가짜: 제자리 답장과 스레드 열기를 기록한다."""

    def __init__(
        self, thread: FakeThread | None = None, reply_error: Exception | None = None
    ) -> None:
        self.thread = thread if thread is not None else FakeThread()
        self.reply_error = reply_error
        self.replies: list[str] = []
        self.opened: list[str] = []

    async def reply_in_place(self, text: str) -> None:
        if self.reply_error is not None:
            raise self.reply_error
        self.replies.append(text)

    async def open_thread(self, name: str) -> AnswerThread:
        self.opened.append(name)
        return self.thread


def incoming(**overrides: Any) -> IncomingMessage:
    base: dict[str, Any] = {
        "message_id": "1000",
        "author_id": "500",
        "author_is_bot": False,
        "is_system": False,
        "guild_id": "100",
        "channel_id": "200",
        "parent_channel_id": None,
        "content": "<@999> 질문",
        "has_attachments": False,
    }
    base.update(overrides)
    return IncomingMessage(**base)


async def wait_until(predicate: Callable[[], bool], timeout_s: float = 5.0) -> None:
    """조건이 참이 될 때까지 10ms 간격으로 확인한다. 넘으면 `AssertionError`."""
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("기다리던 조건이 시간 안에 참이 되지 않았습니다")
        await asyncio.sleep(0.01)


class FakeGateway:
    """`Gateway` 가짜. `run`은 `on_ready` 뒤 `ready`를 세우고 `close()`될 때까지 기다린다."""

    def __init__(
        self,
        bot_user_id: str = "999",
        fail: BaseException | None = None,
        return_early: bool = False,
    ) -> None:
        self.bot_user_id = bot_user_id
        self.fail = fail
        self.return_early = return_early
        self.ready = asyncio.Event()
        self.closed = False
        self.events: list[str] = []
        self._handler: GatewayHandler | None = None
        self._close = asyncio.Event()

    async def run(self, handler: GatewayHandler) -> None:
        self._handler = handler
        if self.fail is not None:
            raise self.fail
        await handler.on_ready(self.bot_user_id)
        self.events.append("ready")
        self.ready.set()
        if self.return_early:
            return
        await self._close.wait()

    async def close(self) -> None:
        self.closed = True
        self.events.append("close")
        self._close.set()

    async def reconnect(self) -> None:
        assert self._handler is not None
        await self._handler.on_ready(self.bot_user_id)

    async def deliver(self, message: IncomingMessage, replies: MessageReplies) -> None:
        assert self._handler is not None
        await self._handler.on_message(message, replies)
