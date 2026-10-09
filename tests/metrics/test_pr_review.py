"""사람이 고친 양(M1) 수집. 실제 git 저장소로 numstat을 확인한다."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

import team_wiki_engine.metrics.pr_review as pr_review_module
from team_wiki_engine.git.client import GitClient
from team_wiki_engine.github.client import GitHubError
from team_wiki_engine.metrics.pr_review import MAX_REVIEWS_PER_RUN, collect_pr_reviews
from team_wiki_engine.metrics.record import STATE_FILES, PrReviewRecord, read_jsonl
from team_wiki_engine.sync.pr_port import (
    OpenPrRequest,
    PrOutcome,
    PrPort,
    TrackedPr,
    read_tracking,
    write_tracking,
)
from team_wiki_engine.sync.types import SyncKind
from tests.helpers.git import commit_files, init_repo

NOW = datetime(2026, 10, 7, 0, 0, tzinfo=UTC)
AT = "2026-10-07T09:00:00+09:00"
SHA_A = "a" * 40
SHA_B = "b" * 40


class _Crash(BaseException):
    """프로세스가 죽는 것을 흉내 낸다(KeyboardInterrupt는 이벤트 루프를 깨뜨려 쓰지 않는다)."""


class _Port:
    def __init__(self, outcomes: dict[str, PrOutcome | BaseException]) -> None:
        self.outcomes = outcomes
        self.outcome_calls: list[str] = []
        self.fetched: list[str] = []
        self.fetch_error: Exception | None = None

    async def find_open(self, kind: SyncKind) -> str | None:
        return None

    async def open(self, req: OpenPrRequest) -> TrackedPr:
        raise AssertionError("쓰지 않는다")

    async def outcome(self, pr: TrackedPr) -> PrOutcome:
        self.outcome_calls.append(pr.id)
        found = self.outcomes[pr.id]
        if isinstance(found, BaseException):
            raise found
        return found

    async def fetch_final_head(self, pr: TrackedPr) -> None:
        self.fetched.append(pr.id)
        if self.fetch_error is not None:
            raise self.fetch_error


def _port(outcomes: dict[str, PrOutcome | BaseException]) -> _Port:
    return _Port(outcomes)


def _tracked(pr_id: str, sha: str, kind: SyncKind = "code") -> TrackedPr:
    return TrackedPr(id=pr_id, kind=kind, branch=f"b{pr_id}", bot_head_sha=sha)


def _merged(final: str | None, closed: str | None = None) -> PrOutcome:
    return PrOutcome(state="merged", final_head_sha=final, closed_at=closed)


async def _repo(root: Path) -> tuple[GitClient, str, str]:
    git = await init_repo(root, {"wiki/a.md": "a\n"})
    bot = await commit_files(git, {"wiki/a.md": "a\nbot\n"}, "bot")
    human = await commit_files(
        git,
        {"wiki/a.md": "a\nbot\nhuman1\nhuman2\n", "wiki/b.md": "b\n", "raw/r.md": "r\n"},
        "human",
    )
    return git, bot, human


def _as_port(port: _Port) -> PrPort:
    return port


async def test_닫힌_PR을_기록하고_추적에서_뺀다_열린_PR과_조회_실패는_남긴다(tmp_dir: Path) -> None:
    git, bot, human = await _repo(tmp_dir / "repo")
    state = tmp_dir / "state"
    write_tracking(state, [_tracked(str(n), bot) for n in (1, 2, 3, 4)])
    port = _port(
        {
            "1": _merged(human, "2026-10-07T00:00:00Z"),
            "2": PrOutcome(state="rejected", final_head_sha=None, closed_at=None),
            "3": PrOutcome(state="open", final_head_sha=None, closed_at=None),
            "4": GitHubError("network", 0),
        }
    )
    records = await collect_pr_reviews(state_dir=state, pr_port=_as_port(port), git=git, now=NOW)
    assert records == [
        PrReviewRecord(
            at=AT,
            pr="1",
            kind="code",
            outcome="merged",
            human_changed_files=2,
            human_additions=3,
            human_deletions=0,
            closed_at="2026-10-07T09:00:00+09:00",
        ),
        PrReviewRecord(
            at=AT,
            pr="2",
            kind="code",
            outcome="rejected",
            human_changed_files=None,
            human_additions=None,
            human_deletions=None,
            closed_at=None,
        ),
    ]
    assert [p.id for p in read_tracking(state)] == ["3", "4"]
    assert len(read_jsonl(state / STATE_FILES.pr_review, PrReviewRecord)) == 2
    assert port.fetched == ["1"]


async def test_최종_커밋을_비교할_수_없으면_None으로_남긴다(tmp_dir: Path) -> None:
    git = await init_repo(tmp_dir / "repo", {"wiki/a.md": "a"})
    state = tmp_dir / "state"
    write_tracking(state, [_tracked("1", SHA_A, "ingest")])
    port = _port({"1": _merged(SHA_B)})
    [record] = await collect_pr_reviews(state_dir=state, pr_port=_as_port(port), git=git, now=NOW)
    assert record.outcome == "merged"
    assert record.human_changed_files is None
    assert record.human_additions is None
    assert record.human_deletions is None
    assert port.fetched == ["1"]


async def test_final_head_sha가_SHA가_아니면_가져오지_않고_측정_불가(tmp_dir: Path) -> None:
    git, bot, _ = await _repo(tmp_dir / "repo")
    state = tmp_dir / "state"
    write_tracking(state, [_tracked("1", bot), _tracked("2", bot), _tracked("3", bot)])
    port = _port({"1": _merged("zzz"), "2": _merged(None), "3": _merged(SHA_A.upper() + "\n")})
    records = await collect_pr_reviews(state_dir=state, pr_port=_as_port(port), git=git, now=NOW)
    assert [r.outcome for r in records] == ["merged"] * 3
    assert all(r.human_changed_files is None for r in records)
    assert port.fetched == []
    assert read_tracking(state) == []


async def test_가져오기가_실패해도_측정_불가로_기록한다(tmp_dir: Path) -> None:
    git, bot, human = await _repo(tmp_dir / "repo")
    state = tmp_dir / "state"
    write_tracking(state, [_tracked("1", bot)])
    port = _port({"1": _merged(human)})
    port.fetch_error = RuntimeError("boom")
    [record] = await collect_pr_reviews(state_dir=state, pr_port=_as_port(port), git=git, now=NOW)
    assert record.human_changed_files is None


async def test_outcome이_GitHubError를_던지면_추적에_남는다(tmp_dir: Path) -> None:
    git, bot, _ = await _repo(tmp_dir / "repo")
    state = tmp_dir / "state"
    write_tracking(state, [_tracked("7", bot)])
    port = _port({"7": GitHubError("rate limit", 403, True)})
    assert await collect_pr_reviews(state_dir=state, pr_port=_as_port(port), git=git, now=NOW) == []
    assert [p.id for p in read_tracking(state)] == ["7"]
    assert not (state / STATE_FILES.pr_review).exists()


async def test_엉뚱한_예외와_엉뚱한_반환값도_추적에_남는다(tmp_dir: Path) -> None:
    git, bot, _ = await _repo(tmp_dir / "repo")
    state = tmp_dir / "state"
    write_tracking(state, [_tracked("1", bot), _tracked("2", bot)])
    port = _port({"1": ValueError("x"), "2": PrOutcome("weird", None, None)})  # type: ignore[arg-type]
    assert await collect_pr_reviews(state_dir=state, pr_port=_as_port(port), git=git, now=NOW) == []
    assert [p.id for p in read_tracking(state)] == ["1", "2"]


async def test_closed_at은_KST로_바꿔_저장하고_읽을_수_없으면_None(tmp_dir: Path) -> None:
    git, bot, human = await _repo(tmp_dir / "repo")
    state = tmp_dir / "state"
    write_tracking(state, [_tracked(str(n), bot) for n in (1, 2, 3)])
    port = _port(
        {
            "1": _merged(human, "2026-10-08T00:00:00Z"),
            "2": _merged(human, "어제"),
            "3": PrOutcome(state="rejected", final_head_sha=None, closed_at="2026-10-08T00:00:00"),
        }
    )
    records = await collect_pr_reviews(state_dir=state, pr_port=_as_port(port), git=git, now=NOW)
    assert [r.closed_at for r in records] == [
        "2026-10-08T09:00:00+09:00",
        None,
        "2026-10-08T09:00:00+09:00",
    ]
    stored = read_jsonl(state / STATE_FILES.pr_review, PrReviewRecord)
    assert stored[0].closed_at == "2026-10-08T09:00:00+09:00"


async def test_추적_파일이_없거나_깨져도_예외가_없다(tmp_dir: Path) -> None:
    git, _, _ = await _repo(tmp_dir / "repo")
    state = tmp_dir / "state"
    port = _port({})
    assert await collect_pr_reviews(state_dir=state, pr_port=_as_port(port), git=git, now=NOW) == []
    (state / STATE_FILES.pr_tracking).write_bytes(b"\xff{{{")
    assert await collect_pr_reviews(state_dir=state, pr_port=_as_port(port), git=git, now=NOW) == []
    assert port.outcome_calls == []


async def test_한_번에_처리하는_PR_수에_상한이_있고_나머지는_추적에_남는다(tmp_dir: Path) -> None:
    git, bot, _ = await _repo(tmp_dir / "repo")
    state = tmp_dir / "state"
    total = MAX_REVIEWS_PER_RUN + 5
    write_tracking(state, [_tracked(str(n), bot) for n in range(1, total + 1)])
    rejected = PrOutcome(state="rejected", final_head_sha=None, closed_at=None)
    port = _port({str(n): rejected for n in range(1, total + 1)})
    records = await collect_pr_reviews(state_dir=state, pr_port=_as_port(port), git=git, now=NOW)
    assert len(records) == MAX_REVIEWS_PER_RUN
    assert [p.id for p in read_tracking(state)] == [str(n) for n in range(total - 4, total + 1)]
    assert len(port.outcome_calls) == MAX_REVIEWS_PER_RUN


REJECTED = PrOutcome(state="rejected", final_head_sha=None, closed_at=None)
OPEN = PrOutcome(state="open", final_head_sha=None, closed_at=None)


def _recorded(state: Path) -> list[str]:
    return [r.pr for r in read_jsonl(state / STATE_FILES.pr_review, PrReviewRecord)]


async def test_취소와_중단이_있어도_기록이_중복되거나_빠지지_않는다(tmp_dir: Path) -> None:
    git, bot, _ = await _repo(tmp_dir / "repo")
    state = tmp_dir / "state"
    write_tracking(state, [_tracked(str(n), bot) for n in (1, 2, 3)])
    port = _port({"1": REJECTED, "2": asyncio.CancelledError(), "3": REJECTED})
    with pytest.raises(asyncio.CancelledError):
        await collect_pr_reviews(state_dir=state, pr_port=_as_port(port), git=git, now=NOW)
    assert _recorded(state) == ["1"]
    assert [p.id for p in read_tracking(state)] == ["2", "3"]  # 1은 이미 추적에서 빠졌다

    port.outcomes.update({"2": REJECTED, "3": _Crash()})
    with pytest.raises(_Crash):
        await collect_pr_reviews(state_dir=state, pr_port=_as_port(port), git=git, now=NOW)
    assert _recorded(state) == ["1", "2"]
    assert [p.id for p in read_tracking(state)] == ["3"]

    port.outcomes["3"] = REJECTED
    await collect_pr_reviews(state_dir=state, pr_port=_as_port(port), git=git, now=NOW)
    assert _recorded(state) == ["1", "2", "3"]
    assert read_tracking(state) == []


async def test_기록은_썼는데_추적을_못_줄이고_죽어도_다시_돌리면_중복이_없다(
    tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git, bot, _ = await _repo(tmp_dir / "repo")
    state = tmp_dir / "state"
    write_tracking(state, [_tracked(str(n), bot) for n in (1, 2)])
    port = _port({"1": REJECTED, "2": REJECTED})
    real = write_tracking
    calls: list[int] = []

    def crashing(directory: Path, prs: object) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise _Crash
        real(directory, prs)  # type: ignore[arg-type]

    monkeypatch.setattr(pr_review_module, "write_tracking", crashing)
    with pytest.raises(_Crash):
        await collect_pr_reviews(state_dir=state, pr_port=_as_port(port), git=git, now=NOW)
    assert _recorded(state) == ["1"]
    assert [p.id for p in read_tracking(state)] == ["1", "2"]  # 추적은 그대로 남았다
    monkeypatch.setattr(pr_review_module, "write_tracking", real)
    port.outcome_calls.clear()
    again = await collect_pr_reviews(state_dir=state, pr_port=_as_port(port), git=git, now=NOW)
    assert _recorded(state) == ["1", "2"]
    assert [r.pr for r in again] == ["2"]
    assert port.outcome_calls == ["2"]  # 이미 기록된 PR은 다시 묻지 않는다
    assert read_tracking(state) == []


async def test_기록이_있는_PR은_다시_기록하지_않고_추적에서만_뺀다(tmp_dir: Path) -> None:
    git, bot, _ = await _repo(tmp_dir / "repo")
    state = tmp_dir / "state"
    write_tracking(state, [_tracked("1", bot)])
    port = _port({"1": REJECTED})
    await collect_pr_reviews(state_dir=state, pr_port=_as_port(port), git=git, now=NOW)
    write_tracking(state, [_tracked("1", bot), _tracked("2", bot, "ingest")])
    port.outcomes["2"] = REJECTED
    records = await collect_pr_reviews(state_dir=state, pr_port=_as_port(port), git=git, now=NOW)
    assert [r.pr for r in records] == ["2"]
    assert _recorded(state) == ["1", "2"]
    assert read_tracking(state) == []


async def test_열린_PR이_100개_넘게_있어도_뒤의_PR이_굶지_않는다(tmp_dir: Path) -> None:
    git, bot, _ = await _repo(tmp_dir / "repo")
    state = tmp_dir / "state"
    total = MAX_REVIEWS_PER_RUN + 50
    write_tracking(state, [_tracked(str(n), bot) for n in range(1, total + 1)])
    port = _port(
        {str(n): OPEN if n <= MAX_REVIEWS_PER_RUN else REJECTED for n in range(1, total + 1)}
    )
    first = await collect_pr_reviews(state_dir=state, pr_port=_as_port(port), git=git, now=NOW)
    assert first == []
    ids = [p.id for p in read_tracking(state)]
    assert ids[:50] == [
        str(n) for n in range(MAX_REVIEWS_PER_RUN + 1, total + 1)
    ]  # 처리 안 한 쪽이 앞
    assert len(ids) == total
    second = await collect_pr_reviews(state_dir=state, pr_port=_as_port(port), git=git, now=NOW)
    assert len(second) == 50
    assert len(read_tracking(state)) == MAX_REVIEWS_PER_RUN


async def test_조회_실패가_계속되는_PR도_뒤로_밀려_나머지를_막지_않는다(tmp_dir: Path) -> None:
    git, bot, _ = await _repo(tmp_dir / "repo")
    state = tmp_dir / "state"
    write_tracking(state, [_tracked("1", bot), _tracked("2", bot)])
    port = _port({"1": GitHubError("down", 500), "2": REJECTED})
    await collect_pr_reviews(state_dir=state, pr_port=_as_port(port), git=git, now=NOW)
    assert [p.id for p in read_tracking(state)] == ["1"]
    assert _recorded(state) == ["2"]


async def test_기록_파일이_디렉터리면_PR을_추적에_남긴다(tmp_dir: Path) -> None:
    git, bot, _ = await _repo(tmp_dir / "repo")
    state = tmp_dir / "state"
    write_tracking(state, [_tracked("1", bot), _tracked("2", bot)])
    (state / STATE_FILES.pr_review).mkdir()
    port = _port({"1": REJECTED, "2": REJECTED})
    records = await collect_pr_reviews(state_dir=state, pr_port=_as_port(port), git=git, now=NOW)
    assert records == []
    assert [p.id for p in read_tracking(state)] == ["1", "2"]


async def test_기록_파일이_링크면_따라가지_않고_PR을_추적에_남긴다(tmp_dir: Path) -> None:
    git, bot, _ = await _repo(tmp_dir / "repo")
    state = tmp_dir / "state"
    write_tracking(state, [_tracked("1", bot)])
    outside = tmp_dir / "outside.jsonl"
    outside.write_text("", encoding="utf-8")
    (state / STATE_FILES.pr_review).symlink_to(outside)
    port = _port({"1": REJECTED})
    records = await collect_pr_reviews(state_dir=state, pr_port=_as_port(port), git=git, now=NOW)
    assert records == []
    assert outside.read_text(encoding="utf-8") == ""
    assert [p.id for p in read_tracking(state)] == ["1"]
